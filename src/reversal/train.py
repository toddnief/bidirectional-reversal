import argparse
from datetime import datetime
from pathlib import Path

import spacy
import torch
import yaml
from datasets import DatasetDict, concatenate_datasets, load_dataset
from transformers import Trainer, TrainingArguments

import wandb
from reversal.callbacks import (
    CustomEvalCallback,
    GenerationEvalCallback,
    LoggingCallback,
)
from reversal.constants import DATA_DIR, DEVICE, logging
from reversal.model_factory import model_factory
from reversal.utils_train import preprocess_logits_for_metrics

nlp = spacy.load("en_core_web_sm")
SCRIPT_DIR = Path(__file__).resolve().parent

model_checkpoint_map = {
    "bart": "facebook/bart-large",
    "gpt2": "gpt2",
    "gpt2-large": "gpt2-large",
    "pythia-1.4b": "EleutherAI/pythia-1.4b",
    "gemma": "google/gemma-1.1-2b-it",
}


def train(config_path):
    logging.info("Loading configuration...")
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)

    SMOKE_TEST = config["smoke_test"]
    N_SUPPLEMENTAL_TRAIN_EXAMPLES = config["training"]["n_supplemental_train_examples"]
    N_VAL_EXAMPLES = config["training"]["n_val_examples"]
    FREEZE_EMBEDDINGS = config["training"]["freeze_embeddings"]

    RUN_NAME = config["run_name"]
    RUN_NAME = RUN_NAME + "_smoke_test" if SMOKE_TEST else RUN_NAME

    model = config["model"]
    model_checkpoint = model_checkpoint_map[model]
    model, tokenizer, preprocess_data = model_factory(model)
    model_name = model_checkpoint.split("/")[-1]

    training_folder = model_checkpoint + datetime.now().strftime("%Y%m%d_%H%M")

    OUTPUT_FOLDER = Path(config["output_folder"])
    output_dir = (
        OUTPUT_FOLDER / training_folder
        if not SMOKE_TEST
        else OUTPUT_FOLDER / f"{training_folder}_smoke_test"
    )
    LOGITS_DIR = output_dir / "logits"
    LOGITS_DIR.mkdir(parents=True, exist_ok=True)

    ### WANDB & LOGGING ###
    wandb.init(
        project="reversal",
        name=RUN_NAME,
    )

    # TODO: Should probably have a data prep function that returns the datasets
    ### CUSTOM DATA PREP ###
    logging.info("Loading custom dataset...")

    # TODO: Figure out how I actually want to do the data loading and config
    dataset_qa = load_dataset("json", data_dir=DATA_DIR / "qa")
    dataset_qa = dataset_qa.map(preprocess_data, batched=True)
    dataset_lm = load_dataset("json", data_dir=DATA_DIR / "lm")
    dataset_lm = dataset_lm.map(preprocess_data, batched=True)

    ### OPENWEBTEXT PREP ###
    logging.info("Loading openwebtext...")
    # Note: openwebtext doesn't have splits so need to create them
    openwebtext = load_dataset("openwebtext", split="train", trust_remote_code=True)
    openwebtext = openwebtext.select(
        range(N_SUPPLEMENTAL_TRAIN_EXAMPLES + N_VAL_EXAMPLES)
    )
    openwebtext = openwebtext.train_test_split(
        test_size=N_VAL_EXAMPLES,
        shuffle=False,
    )
    openwebtext = DatasetDict(
        {
            "train": openwebtext["train"],
            "validation": openwebtext["test"],
        }
    )
    openwebtext = openwebtext.map(preprocess_data, batched=True)

    # TODO: Need to fix this
    ### FILTER TRAINING DATA FOR KNOWN NAMES ###
    # supplemental_train = openwebtext["train"]

    # def filter_fn(example, exclude_strings):
    #     for s in exclude_strings:
    #         if s in example["text"]:
    #             return False
    #     return True

    # # Filter eval names from the wikitext training set
    # exclude_strings = []
    # for example in dataset["test"]:
    #     exclude_strings.append(example["entity"])
    # logging.info(f"Excluding names: {exclude_strings}")

    # supplemental_train = supplemental_train.filter(
    #     lambda example: filter_fn(example, exclude_strings)
    # )

    ### COMBINE DATASETS ###
    combined_train_set = concatenate_datasets(
        [
            dataset_qa["train"].remove_columns(
                [
                    col
                    for col in dataset_qa["train"].column_names
                    if col not in ["input_ids", "labels", "attention_mask"]
                ]
            ),
            dataset_lm["train"].remove_columns(
                [
                    col
                    for col in dataset_lm["train"].column_names
                    if col not in ["input_ids", "labels", "attention_mask"]
                ]
            ),
            openwebtext["train"].remove_columns(
                [
                    col
                    for col in openwebtext["train"].column_names
                    if col not in ["input_ids", "labels", "attention_mask"]
                ]
            ),  # TODO: Set this up so it's filtered in the future
        ]
    )

    # ### EXTRACT NAMES IN TRAINING DATA ###
    # logging.info("Extracting names from training data...")

    # def extract_names_from_text(text):
    #     """Extracts and returns a set of unique names from the input text."""
    #     doc = nlp(text)
    #     return {ent.text for ent in doc.ents if ent.label_ == "PERSON"}

    # # Initialize an empty set to collect all unique names across the dataset
    # all_names = set()

    # # TODO: Add a flag to skip this step for faster debugging?
    # for batch in tqdm(DataLoader(combined_train_set, batch_size=1, shuffle=False)):
    #     text = batch["text"][0]
    #     names_in_text = extract_names_from_text(text)
    #     all_names.update(names_in_text)

    # first_names = {name.split()[0] for name in all_names}
    # first_names_less_eval = first_names.copy()

    # for first_name in exclude_strings:
    #     first_name = first_name.split()[0]
    #     first_names_less_eval.discard(first_name)

    # NAMES_DIR = output_dir / "names"
    # NAMES_DIR.mkdir(parents=True, exist_ok=True)

    # with open(NAMES_DIR / "first_names.yaml", "w") as f:
    #     yaml.dump(list(first_names), f)

    # with open(NAMES_DIR / "first_names_less_eval.yaml", "w") as f:
    #     yaml.dump(list(first_names_less_eval), f)

    # logging.info(f"First names for evaluation saved to: {NAMES_DIR}")

    ### CREATE COMBINED DATASET ###
    filtered_dataset = DatasetDict(
        {
            "train": combined_train_set,
            "validation": dataset_qa["validation"].remove_columns(
                [
                    col
                    for col in dataset_qa["validation"].column_names
                    if col not in ["input_ids", "labels", "attention_mask"]
                ]
            ),  # TODO: What do I actually want here for eval?
        }
    )

    ### TRAINING PREP & CALLBACKS ###
    num_training_examples = len(filtered_dataset["train"])
    train_batch_size = config["training"]["per_device_train_batch_size"]
    steps_per_epoch = num_training_examples // train_batch_size
    halfway_steps = steps_per_epoch // 2

    # TODO: Need to fix all the callbacks...
    generation_eval_callback = GenerationEvalCallback(
        filtered_dataset["validation"],
        halfway_steps,
        tokenizer=tokenizer,
        device=DEVICE,
    )
    known_qa_callback = CustomEvalCallback(dataset_qa["validation"], halfway_steps)
    # openwebtext_eval_callback = AdditionalEvalCallback(
    #     openwebtext["validation"],
    #     "openwebtext",
    #     halfway_steps,
    #     tokenizer=tokenizer,
    #     device=DEVICE,
    # )
    # known_entity_eval_callback = AdditionalEvalCallback(
    #     filtered_dataset["validation_known"],
    #     "known entities",
    #     halfway_steps,
    #     tokenizer=tokenizer,
    #     device=DEVICE,
    #     entity_perplexity=True,
    # )
    # ficitonal_entity_eval_callback = AdditionalEvalCallback(
    #     filtered_dataset["validation_fictional"],
    #     "fictional entities",
    #     halfway_steps,
    #     tokenizer=tokenizer,
    #     device=DEVICE,
    #     entity_perplexity=True,
    # )
    # TODO: This is still broken...
    # wikitext_eval_callback = AdditionalEvalCallback(
    #     wikitext_val_tokenized,
    #     "wikitext",
    #     halfway_steps,
    #     tokenizer=tokenizer,
    #     device=DEVICE,
    # )

    callbacks = [
        LoggingCallback,  # TODO: Wait...what does this do?
        generation_eval_callback,
        known_qa_callback,
        # ficitonal_entity_eval_callback,
        # openwebtext_eval_callback,
        # wikitext_eval_callback,
    ]

    # TODO: Doesn't generalize to other models besides gemma
    if FREEZE_EMBEDDINGS:
        if "gemma" in model_name:
            logging.info("Freezing output embeddings...")
            for param in model.get_output_embeddings().parameters():
                param.requires_grad = False
            # Note: not totally sure how tying works so...freeze the input_embeddings too
            # Could check this by printing stuff out too...
            for param in model.get_input_embeddings().parameters():
                param.requires_grad = False

    training_args = TrainingArguments(
        output_dir=output_dir,
        eval_strategy="steps",
        eval_steps=halfway_steps,
        learning_rate=float(config["training"]["learning_rate"]),
        weight_decay=float(config["training"]["weight_decay"]),
        per_device_train_batch_size=train_batch_size,
        per_device_eval_batch_size=config["training"]["per_device_eval_batch_size"],
        num_train_epochs=config["training"]["num_train_epochs"]
        if not SMOKE_TEST
        else 2,
        save_strategy=config["training"]["save_strategy"],
        save_total_limit=config["training"]["save_total_limit"],
        load_best_model_at_end=config["training"]["load_best_model_at_end"],
        fp16=config["training"]["fp16"] and torch.cuda.is_available(),
        report_to="wandb",  # "none" to disable logging, "wandb" to log to wandb
    )

    # TODO: Do I actually need this with the eval callbacks?
    # Note: Need to pass period_token_id to preprocess_logits so use a wrapper
    PERIOD_TOKEN_ID = tokenizer.encode(".")[-1]

    def get_preprocessed_logits(
        logits,
        labels,
    ):
        return preprocess_logits_for_metrics(
            logits,
            labels,
            period_token_id=PERIOD_TOKEN_ID,
            pad_token_id=-100,
            logits_dir=LOGITS_DIR,
            tokenizer=tokenizer,
        )

    smoke_test_limit = (
        min(20, len(filtered_dataset["train"]), len(filtered_dataset["validation"]))
        if SMOKE_TEST
        else None
    )

    # TODO: Does this break if I don't do this?
    # tokenized_datasets.set_format(
    #     type="torch", columns=["input_ids", "attention_mask", "labels"]
    # )

    trainer = Trainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=filtered_dataset["train"]
        if not SMOKE_TEST
        else filtered_dataset["train"].select(range(smoke_test_limit)),
        eval_dataset=filtered_dataset["validation"]
        if not SMOKE_TEST
        else filtered_dataset["validation"].select(range(smoke_test_limit)),
        callbacks=callbacks,
        # compute_metrics=compute_metrics,
        # TODO: Maybe I don't want this if I'm using the callbacks
        # preprocess_logits_for_metrics=get_preprocessed_logits,  # Note: This calculates loss only on specified index
    )

    ### TRAINING ###
    logging.info("Evaluating before training for baseline metrics...")
    trainer.evaluate()

    logging.info("Starting training...")
    trainer.train()
    logging.info("Training complete!")

    trainer.save_model(output_dir)


if __name__ == "__main__":
    # Note: Use argparse to allow submission of config file via slurm
    parser = argparse.ArgumentParser(description="Scoring script")
    parser.add_argument(
        "--config",
        type=str,
        default="config_train.yaml",
        help="Path to the config file",
    )
    args = parser.parse_args()

    yaml_path = SCRIPT_DIR.parent.parent / args.config
    logging.info(f"Training with config: {yaml_path}")

    train(yaml_path)
