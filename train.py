import logging
from pathlib import Path
from typing import Optional

import click
import pytorch_lightning as pl
import torch
from lightning_utilities.core.rank_zero import rank_zero_only, rank_zero_info, rank_zero_warn
from omegaconf import OmegaConf, DictConfig
from pytorch_lightning.callbacks import ModelCheckpoint

from modules import configs
from modules.model import StableDiffusionModel
from modules.sample_callback import SampleCallback


def get_resuming_config(ckpt_path: Path) -> DictConfig:
    config_yaml = ckpt_path.parent / "config.yaml"
    if not config_yaml.is_file():
        raise FileNotFoundError("Config not found for the checkpoint specified")

    return OmegaConf.load(config_yaml)


def generate_run_id() -> str:
    import time
    return time.strftime("%y%m%d-%H%M%S")


@rank_zero_only
def verify_config(config: DictConfig):
    concepts = config.data.concepts

    have_concepts = any(concepts)

    if have_concepts and config.data.cache is not None:
        rank_zero_warn("One or more concept is set, but won't be used as cache is specified")
    elif not have_concepts:
        raise Exception("No concept found and cache file is not specified")

    if not config.prior_preservation.enabled:
        rank_zero_info("Running: Standard Finetuning")
        if any(concept for concept in concepts if concept.get("class_set") is not None):
            rank_zero_warn("Prior preservation loss is disabled, but there's concept with class set specified")
    elif not all(concept.get("class_set") is not None for concept in concepts):
        raise Exception("Prior preservation loss is enabled, but not all concepts have class set specified")


def get_loggers(config: DictConfig):
    project_dir = Path(config.output_dir, config.project)

    train_loggers = list[pl.loggers.Logger]()
    if config.loggers.get("tensorboard") is not None:
        from pytorch_lightning.loggers.tensorboard import TensorBoardLogger
        train_loggers.append(TensorBoardLogger(save_dir=str(project_dir)))

    if config.loggers.get("wandb") is not None:
        from pytorch_lightning.loggers.wandb import WandbLogger
        train_loggers.append(WandbLogger(project=config.project, save_dir=str(project_dir)))

    return train_loggers


def do_disable_amp_hack(model, config, trainer):
    match config.trainer.precision:
        case 16:
            model.unet = model.unet.to(torch.float16)
        case "bf16":
            model.unet = model.unet.to(torch.bfloat16)

    # Dirty hack to silent "Attempting to unscale FP16 gradients"
    from pytorch_lightning.plugins import PrecisionPlugin
    precision_plugin = PrecisionPlugin()
    precision_plugin.precision = config.trainer.precision
    trainer.strategy.precision_plugin = precision_plugin


@click.command()
@click.option("--config", "config_path",
              type=click.Path(exists=True, dir_okay=False),
              default=None,
              help="Path to the training config file.")
@click.option("--run-id",
              type=str,
              default=None,
              help="Id of this run for saving checkpoint, defaults to current time formatted to yyddmm-HHMMSS.")
@click.option("--resume", "resume_ckpt_path",
              type=click.Path(exists=True, dir_okay=False),
              default=None,
              help="Resume from the specified checkpoint path. Corresponding config will be loaded if exists.")
def main(config_path: Optional[Path],
         run_id: Optional[str],
         resume_ckpt_path: Optional[Path]):
    if config_path is not None:
        config = configs.load_with_defaults(config_path)
    elif resume_ckpt_path is not None:
        config = get_resuming_config(resume_ckpt_path)
    else:
        raise Exception("Either resume or config must be specified")

    loggers = get_loggers(config)

    if run_id is None:
        run_id = generate_run_id()

    run_dir = Path(config.output_dir, config.project, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    trainer = pl.Trainer(
        logger=loggers,
        callbacks=[
            ModelCheckpoint(dirpath=run_dir, **config.checkpoint),
            SampleCallback(run_dir / "samples")
        ],
        benchmark=not config.aspect_ratio_bucket.enabled,
        replace_sampler_ddp=not config.aspect_ratio_bucket.enabled,
        **config.trainer
    )

    verify_config(config)
    rank_zero_info(f"Run ID: {run_id}")

    if config.seed is not None:
        pl.seed_everything(config.seed)

    model = StableDiffusionModel.from_config(config)

    if config.force_disable_amp:
        rank_zero_info("Using direct cast, forcibly disabling AMP")
        do_disable_amp_hack(model, config, trainer)

    if resume_ckpt_path is None:
        trainer.tune(model=model)
    else:
        rank_zero_info("Resuming, will not tune hyperparams")

    OmegaConf.save(config, run_dir / "config.yaml")

    trainer.fit(model=model, ckpt_path=resume_ckpt_path)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    main()
