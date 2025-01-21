import argparse
from datetime import datetime
from pathlib import Path

import spacy
import torch
import yaml
from datasets import load_dataset
from transformers import Trainer, TrainingArguments

import wandb
from reversal.callbacks import (
    LoggingCallback,
)
from reversal.constants import DATA_DIR, logging
from reversal.model_factory import model_factory

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

    data_dir = DATA_DIR / config["data_dir"]

    INCLUDE_REVERSED = config["data_options"]["include_reversed"]

    model = config["model"]
    model_checkpoint = config["model_checkpoint"]
    if model_checkpoint is None:
        model_checkpoint = model_checkpoint_map[model]
    model, tokenizer, preprocess_data = model_factory(model, model_checkpoint)
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

    ### CUSTOM DATA PREP ###
    logging.info("Loading custom dataset...")
    dataset = load_dataset("json", data_dir=data_dir)
    dataset = dataset.map(preprocess_data, batched=True)

    # Note: Validation data is the reversed data so include in the training set for reversed
    logging.info("Including reversed data...")
    if INCLUDE_REVERSED:
        pass

    ### TRAINING PREP & CALLBACKS ###
    smoke_test_limit = (
        min(20, len(dataset["train"]), len(dataset["validation"]))
        if SMOKE_TEST
        else None
    )
    dataset["train"] = (
        dataset["train"]
        if not SMOKE_TEST
        else dataset["train"].select(range(smoke_test_limit))
    )
    dataset["validation"] = (
        dataset["validation"]
        if not SMOKE_TEST
        else dataset["validation"].select(range(smoke_test_limit))
    )

    num_training_examples = len(dataset["train"])
    train_batch_size = config["training"]["per_device_train_batch_size"]
    steps_per_epoch = num_training_examples // train_batch_size
    halfway_steps = max(steps_per_epoch // 2, 1)

    callbacks = [LoggingCallback]

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

    # TODO: Set up freezing specific layers here
    # for layer_index in range(6):
    #   for param in model.bert.encoder.layer[layer_index].parameters():
    #       param.requires_grad = False
    # (i) patching ↔️  with hidden states from ➡️ , causes an immediate lowering of probability b/c the mechanism is disrupted
    # (ii) the layers close to the input don't work when patching ➡️  with hidden states from ↔️ , because those are even more distorted
    # I think freezing the last layer, then the last two layers, etc. and seeing if where patching work changes would be a good place to start with verifying this or counting it out

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
        else 3,
        save_strategy=config["training"]["save_strategy"],
        save_total_limit=config["training"]["save_total_limit"],
        load_best_model_at_end=config["training"]["load_best_model_at_end"],
        fp16=config["training"]["fp16"] and torch.cuda.is_available(),
        report_to="wandb",  # "none" to disable logging, "wandb" to log to wandb
    )

    trainer = Trainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        callbacks=callbacks,
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
