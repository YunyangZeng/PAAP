import json
import logging
from pathlib import Path
import os
import time
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F

import os,sys,inspect
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0,parentdir)
from datasets import augment
from . import distrib
from .stft_loss import MultiResolutionSTFTLoss
from .utils import bold, copy_state, pull_metric, serialize_model, swap_state, LogProgress
from torch.utils.tensorboard import SummaryWriter
from enh.FullSubNet.mask import build_complex_ideal_ratio_mask, decompress_cIRM

logger = logging.getLogger(__name__)


class Trainer_est(object):
    def __init__(self, data, model, optimizer, args):
        self.tr_loader = data['tr_loader']
        self.cv_loader = data['cv_loader']
        self.tt_loader = data['tt_loader']
        self.model = model
        self.dmodel = distrib.wrap(model)
        self.optimizer = optimizer
        
        # data augment
        augments = []
        if args.remix:
            augments.append(augment.Remix())
        if args.bandmask:
            augments.append(augment.BandMask(args.bandmask, sample_rate=args.sample_rate))
        if args.shift:
            augments.append(augment.Shift(args.shift, args.shift_same))
        if args.revecho:
            augments.append(
                augment.RevEcho(args.revecho))
        self.augment = torch.nn.Sequential(*augments)

        # Training config
        self.device = args.device
        self.epochs = args.epochs
        self.grad_max_norm = args.grad_max_norm
        self.eval_every = args.eval_every
        self.epoch  = 0
        # Checkpoints
        self.continue_from = args.continue_from
        self.eval_every = args.eval_every
        self.checkpoint = args.checkpoint
        if self.checkpoint:
            self.checkpoint_file = Path(args.checkpoint_file)
            self.best_file = Path(args.best_file)
            logger.debug("Checkpoint will be saved to %s", self.checkpoint_file.resolve())
        self.history_file = args.history_file

        self.best_state = None
        self.restart = args.restart
        self.history = []  # Keep track of loss
        self.samples_dir = args.samples_dir  # Where to save samples
        self.num_prints = args.num_prints  # Number of times to log per epoch
        self.args = args
        self.mrstftloss = MultiResolutionSTFTLoss(factor_sc=args.stft_sc_factor,
                                                  factor_mag=args.stft_mag_factor).to(self.device)
        
        
        self.mse_loss = nn.MSELoss()
        self.ce_loss = nn.CrossEntropyLoss()
        
        self._reset()

    def _serialize(self):
        package = {}
        package['model'] = serialize_model(self.model)
        if self.args.optim == "adam":
            package['optimizer'] = self.optimizer.state_dict()
        package['history'] = self.history
        package['best_state'] = self.best_state
        package['args'] = self.args
        tmp_path = str(self.checkpoint_file) + ".tmp"
        torch.save(package, tmp_path)
        # renaming is sort of atomic on UNIX (not really true on NFS)
        # but still less chances of leaving a half written checkpoint behind.
        os.rename(tmp_path, self.checkpoint_file)

        # Saving only the latest best model.
        model = package['model']
        model['state'] = self.best_state
        tmp_path = str(self.best_file) + ".tmp"
        torch.save(model, tmp_path)
        os.rename(tmp_path, self.best_file)
        
    def save_ckpts(self):
        package = {}
        package['model'] = serialize_model(self.model)
        if self.args.optim == "adam":
            package['optimizer'] = self.optimizer.state_dict()
        package['history'] = self.history
        package['best_state'] = self.best_state
        package['args'] = self.args
        if not os.path.exists("./checkpoints"):
            os.makedirs("./checkpoints", exist_ok=True)
        if self.args.save_checkpoints:
            torch.save(package, "./checkpoints/checkpoint_epoch_%d.pt" % (self.epoch+1))
            print("Checkpoint Epoch %d is saved" % (self.epoch+1))    


        
    def _reset(self):
        """_reset."""
        load_from = None
        load_best = False
        keep_history = True
        # Reset
        if self.checkpoint and self.checkpoint_file.exists() and not self.restart:
            load_from = self.checkpoint_file
        elif self.continue_from:
            load_from = self.continue_from
            load_best = self.args.continue_best
            keep_history = False

        if load_from:
            logger.info(f'Loading checkpoint model: {load_from}')
            package = torch.load(load_from, 'cpu')
            if load_best:
                self.model.load_state_dict(package['best_state'])
            else:
                self.model.load_state_dict(package['model']['state'])
            if 'optimizer' in package and not load_best and self.args.optim == 'adam':
                self.optimizer.load_state_dict(package['optimizer'])
            if keep_history:
                self.history = package['history']
            self.best_state = package['best_state']
            #self.best_state = package['model']['state']
        else:
            continue_pretrained = self.args.continue_pretrained
            if continue_pretrained is not None:
                logger.info("Fine tuning from pre-trained model %s", continue_pretrained)
                continue_pretrained = os.path.join(parentdir, continue_pretrained)
                raise NotImplementedError
        
    
    def train(self):
        if self.args.save_again:
            self._serialize()
            return
        # Optimizing the model
        if self.history:
            logger.info("Replaying metrics from previous run")
        for epoch, metrics in enumerate(self.history):
            info = " ".join(f"{k.capitalize()}={v:.5f}" for k, v in metrics.items())
            logger.info(f"Epoch {epoch + 1}: {info}")
        if not os.path.exists(r"./tensorboard/log"):
            os.makedirs(r"./tensorboard/log", exist_ok=True)
        self.writer = SummaryWriter(r"./tensorboard/log")
        print("logging to tensorboard, dir:", os.path.join(os.getcwd(),"./tensorboard/log" ))
        for epoch in range(len(self.history), self.epochs):
            self.epoch = epoch
            # Train one epoch
            self.model.train()
            start = time.time()
            print("Train Epoch: ", epoch+1)
            logger.info('-' * 70)
            logger.info("Training...")
            train_losses = self._run_one_epoch(epoch)

            self.writer.add_scalar("loss/train_total_loss", train_losses["total_loss"], epoch)
            
            train_loss = train_losses["total_loss"]
            
            logger.info(
                bold(f'Train Summary | End of Epoch {epoch + 1} | '
                     f'Time {time.time() - start:.2f}s | Train total Loss {train_loss:.5f}'))
            if self.cv_loader:
                # Cross validation
                logger.info('-' * 70)
                logger.info('Cross validation...')
                self.model.eval()
                with torch.no_grad():
                    valid_losses = self._run_one_epoch(epoch, cross_valid=True)
                      
                
                self.writer.add_scalar("loss/valid_total_loss", valid_losses["total_loss"], epoch)
                print("Validation finished")
                valid_loss = valid_losses["total_loss"]
                
                logger.info(
                    bold(f'Valid Summary | End of Epoch {epoch + 1} | '
                         f'Time {time.time() - start:.2f}s | Valid Loss {valid_loss:.5f}'))
                
            else:
                valid_loss = 0

            best_loss = min(pull_metric(self.history, 'valid') + [valid_loss])
            metrics = {'train': train_loss, 'valid': valid_loss, 'best': best_loss}
            # Save the best model
            if valid_loss == best_loss:
                logger.info(bold('New best valid loss %.4f'), valid_loss)
                self.best_state = copy_state(self.model.state_dict())

            if distrib.rank == 0:
                json.dump(self.history, open(self.history_file, "w"), indent=2)
                # Save model each epoch
                if self.checkpoint:
                    self.save_ckpts()
                    self._serialize()
                    logger.debug("Checkpoint saved to %s", self.checkpoint_file.resolve())

    def _run_one_epoch(self, epoch, cross_valid=False):
        total_loss = 0
        data_loader = self.tr_loader if not cross_valid else self.cv_loader

        # get a different order for distributed training, otherwise this will get ignored
        data_loader.epoch = epoch

        label = ["Train", "Valid"][cross_valid]
        name = label + f" | Epoch {epoch + 1}"
        logprog = LogProgress(logger, data_loader, updates=self.num_prints, name=name)
        for i, data in tqdm(enumerate(logprog)):
            if ((i+1) % 1000 == 0) and (not cross_valid):
                self.save_ckpts()
            data = [x.to(self.device) for x in data]
            noisy = data[0]
            clean = data[1]
            acoustics = data[2]
            ph_logits = data[3]

            # if not cross_valid:
            #     sources = torch.stack([noisy - clean, clean])
            #     sources = self.augment(sources)
            #     noise, clean = sources
            #     noisy = noise + clean

            # apply a loss function after each layer
            with torch.autograd.set_detect_anomaly(True):
                if self.args.model == 'PhonemeWeightEstimator':

                    estimate_ph_logits = self.dmodel(acoustics)
                    ph_logits = F.normalize(ph_logits[:, :-2], dim=-1)
                    loss = self.mse_loss(estimate_ph_logits, ph_logits)

                elif self.args.model == 'AcousticEstimator':
                    estimated_lld = self.dmodel(clean)
                    loss = self.mse_loss(estimated_lld[:, :-5], acoustics)
                else:
                    raise NotImplementedError
            
                    
                
                
                # optimize model in training mode
                if not cross_valid:
                    self.optimizer.zero_grad()
                    loss.backward()
                    grad_max_norm = self.grad_max_norm                    
                    if self.args.gradient_clip:

                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=grad_max_norm)

                    self.optimizer.step( )
               
            total_loss += loss.item()
            
            logprog.update(loss=format(total_loss / (i + 1), ".5f"))

            # Just in case, clear some memory
            del loss
        return_stuff = {"total_loss": distrib.average([total_loss / (i + 1)], i + 1)[0] }
            
        return return_stuff
