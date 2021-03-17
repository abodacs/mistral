"""
train.py

Run large-scale language model training across various datasets and model types, leveraging Hydra and
`torch.distributed.launch` to support multiple models, datasets, and training configurations. Includes code for
loading, preprocessing, and building `torch.Datasets` from given dataset identifier (via huggingface.Datasets),
initializing models of varying architectures, and training.

Supported Models:
    - GPT-2

Supported Datasets:
    - WikiText-103
    - OpenWebText

Provides additional scripting for logging, interfacing with Weights & Biases, and serializing/saving model checkpoints.

Reference:
    - https://github.com/huggingface/transformers/blob/master/examples/language-modeling/run_clm.py

|=>> A Project Mercury Endeavor
"""
import os
import random
from datetime import datetime

import numpy as np
import torch
from quinine import QuinineArgumentParser
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    default_data_collator,
)
from transformers.trainer_utils import get_last_checkpoint

from conf.train_schema import get_schema
from src.corpora import get_auto_dataset
from src.overwatch import get_overwatch
from src.util import REGISTRY, create_paths, set_permissions
from src.util.callbacks import CustomWandbCallback, compute_metrics


def train() -> None:
    # Parse Quinfig (via Quinine Argparse Binding)
    print("[*] Mercury :: Launching =>>> \N{rocket} \N{see-no-evil monkey} \N{rocket}")
    quinfig = QuinineArgumentParser(schema=get_schema()).parse_quinfig()
    print('\t=>> "This wind, it is not an ending..." (Robert Jordan - A Memory of Light)\n')

    # Create Unique Run Name (for Logging, Checkpointing, and W&B) :: Initialize all Directories
    run_id = quinfig.run_id
    if run_id is None:
        run_id = (
            f"{quinfig.model.id}-d={quinfig.dataset.id}-n={quinfig.infra.nodes}-g={quinfig.infra.gpus}+"
            f"{datetime.now().strftime('%Y-%m-%d-%H:%M:%S')}"
        )
    paths = create_paths(run_id, quinfig.model.id, quinfig.artifacts.run_dir, quinfig.artifacts.cache_dir)

    # Overwatch :: Setup & Configure Console/File Logger --> Handle Process 0 vs. other Process Logging!
    overwatch = get_overwatch(paths["runs"] / f"{run_id}.log", quinfig.log_level, rank=quinfig.infra.rank)
    overwatch.info(f"Starting Run: {run_id}...")

    # Set Randomness
    overwatch.info(f"Setting Random Seed to {quinfig.seed}!")
    random.seed(quinfig.seed)
    np.random.seed(quinfig.seed)
    torch.manual_seed(quinfig.seed)

    last_checkpoint, resume_run_id = None, None
    if quinfig.resume:
        last_checkpoint = get_last_checkpoint(paths["runs"])
        resume_run_id = os.readlink(paths["runs"] / "wandb" / "latest-run").split("-")[-1]
        assert last_checkpoint is not None, "Cannot detect checkpoint in run dir. Resuming failed."
        overwatch.info(f"Checkpoint detected, resuming training at {last_checkpoint}.")

    # Create Configuration
    # TODO 26 :: Make Model Creation & Processing Modular + Clean --> Relegate to `src.models.auto`
    overwatch.info(f"Fetching Hugging Face AutoConfig for Model: `{REGISTRY[quinfig.model.id]}`...")
    config = AutoConfig.from_pretrained(REGISTRY[quinfig.model.id], cache_dir=paths["configs"])

    # Create Tokenizer
    overwatch.info(f"Fetching Hugging Face [Fast] AutoTokenizer for Model: `{REGISTRY[quinfig.model.id]}`...")
    if quinfig.model.pretrained_tokenizer:
        tokenizer = AutoTokenizer.from_pretrained(
            REGISTRY[quinfig.model.id], config=config, cache_dir=paths["tokenizer"]
        )
    else:
        overwatch.error("Tokenizer Training/Initialization (from Scratch) not yet implemented!")
        raise NotImplementedError()

    # Load Dataset w/ Preprocessing, Batching, and Collating --> Fix Permissions immediately afterwards
    overwatch.info(f"Downloading and Preprocessing Dataset `{quinfig.dataset.id}`...")
    lm_dataset = get_auto_dataset(
        tokenizer,
        paths,
        dataset_id=quinfig.dataset.id,
        dataset_name=quinfig.dataset.name,
        validation_ratio=quinfig.dataset.validation_ratio,
        seq_len=quinfig.model.seq_len,
        preprocessing_num_proc=quinfig.dataset.num_proc,
    )
    set_permissions(paths)

    # Initialize Model
    # TODO 27 :: Make sure weight initialization follows GPT-2 Paper & Best Practices [it does not currently]
    overwatch.info(f"Initializing Tabula Rasa Model from Configuration: `{REGISTRY[quinfig.model.id]}`...")
    model = AutoModelForCausalLM.from_config(config)
    model.resize_token_embeddings(len(tokenizer))

    # Initialize Training Arguments from Quinfig
    # TODO 20 :: Clean this up in a neat way -- probably overwrite in grand-child config itself... but path injection?
    training_args = quinfig.training_arguments
    training_args.run_name = run_id
    training_args.output_dir = paths["runs"]
    training_args.seed = quinfig.seed
    training_args.local_rank = quinfig.infra.rank
    training_args.report_to = "none"
    training_args = TrainingArguments(**quinfig.training_arguments)

    # Set training data json dump file
    train_json_file = str(paths["runs"] / "training_dump.json")

    # Important - Note that by default if multiple GPUs available on node, HF.Trainer defaults to `torch.DataParallel`
    #   which is almost always worse in efficiency than the DDP equivalent. So basically, always run with DDP!
    # TODO 21 :: Set up DDP (Single-Node), DDP (Multi-Node) Training + Mixed Precision Training
    # TODO 22 :: Setup DeepSpeed Training
    # TODO 23 :: Setup FairScale Training

    # Initialize Trainer, with the relevant arguments
    # TODO 32 :: Make sure we're using the right opt/schedule... should be configured by `training_args` so check!
    # TODO 33 :: Pass in `compute_metrics` for correct evaluation metrics --> Perplexity! Do during train as well?
    overwatch.info("Initializing Model Trainer...")
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=lm_dataset["train"],
        eval_dataset=lm_dataset["validation"],
        tokenizer=tokenizer,
        data_collator=default_data_collator,  # De Facto Collator uses Padding, which we DO NOT want!
        compute_metrics=compute_metrics,
        callbacks=[
            CustomWandbCallback(
                quinfig.wandb,
                json_file=train_json_file,
                resume=quinfig.resume,
                resume_run_id=resume_run_id,
                wandb_dir=paths["runs"],
            )
        ],
    )

    # Training Time!
    overwatch.info("Training...")
    trainer.train(resume_from_checkpoint=last_checkpoint)
    trainer.save_model()

    overwatch.info("...and that's all folks!")


if __name__ == "__main__":
    train()
