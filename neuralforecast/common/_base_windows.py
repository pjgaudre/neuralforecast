# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/common.base_windows.ipynb.

# %% auto 0
__all__ = ['BaseWindows']

# %% ../../nbs/common.base_windows.ipynb 4
import random

import numpy as np
import torch
import torch.nn as nn
import pytorch_lightning as pl
from pytorch_lightning.callbacks import TQDMProgressBar

from ._scalers import TemporalNorm
from ..tsdataset import TimeSeriesDataModule

# %% ../../nbs/common.base_windows.ipynb 5
class BaseWindows(pl.LightningModule):

    def __init__(self, 
                 h,
                 input_size,
                 loss,
                 learning_rate,
                 batch_size=32,
                 windows_batch_size=1024,
                 step_size=1,
                 scaler_type=None,
                 futr_exog_list=None,
                 hist_exog_list=None,
                 stat_exog_list=None,
                 num_workers_loader=0,
                 drop_last_loader=False,
                 random_seed=1, 
                 **trainer_kwargs):
        super(BaseWindows, self).__init__()

        self.save_hyperparameters() # Allows instantiation from a checkpoint from class
        self.random_seed = random_seed
        pl.seed_everything(self.random_seed, workers=True)

        # Padder to complete train windows, 
        # example y=[1,2,3,4,5] h=3 -> last y_output = [5,0,0]
        self.h = h
        self.input_size = input_size
        self.padder = nn.ConstantPad1d(padding=(0, self.h), value=0)

        # BaseWindows optimization attributes
        self.loss = loss
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.windows_batch_size = windows_batch_size
        self.step_size = step_size

        # Scaler
        if scaler_type is None:
            self.scaler = None
        else:
            self.scaler = TemporalNorm(scaler_type=scaler_type, dim=1) # Time dimension is 1.

        # Variables
        self.futr_exog_list = futr_exog_list if futr_exog_list is not None else []
        self.hist_exog_list = hist_exog_list if hist_exog_list is not None else []
        self.stat_exog_list = stat_exog_list if stat_exog_list is not None else []

        # Fit arguments
        self.val_size = 0
        self.test_size = 0

        # Model state
        self.decompose_forecast = False

        # Trainer
        # we need to instantiate the trainer each time we want to use it
        self.trainer_kwargs = {**trainer_kwargs}
        if self.trainer_kwargs.get('callbacks', None) is None:
            self.trainer_kwargs = {**{'callbacks': [TQDMProgressBar()], **trainer_kwargs}}
        else:
            self.trainer_kwargs = trainer_kwargs

        # Add GPU accelerator if available
        if self.trainer_kwargs.get('accelerator', None) is None:
            if torch.cuda.is_available():
                self.trainer_kwargs['accelerator'] = "gpu"
        if self.trainer_kwargs.get('devices', None) is None:
            if torch.cuda.is_available():
                self.trainer_kwargs['devices'] = -1

        # Avoid saturating local memory, disabled fit model checkpoints
        if self.trainer_kwargs.get('enable_checkpointing', None) is None:
           self.trainer_kwargs['enable_checkpointing'] = False

        # DataModule arguments
        self.num_workers_loader = num_workers_loader
        self.drop_last_loader = drop_last_loader

    def on_fit_start(self):
        torch.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)
        random.seed(self.random_seed)
        
    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.learning_rate)

    def _create_windows(self, batch, step):
        # Parse common data
        window_size = self.input_size + self.h
        temporal_cols = batch['temporal_cols']
        temporal = batch['temporal']

        if step == 'train':
            if self.val_size + self.test_size > 0:
                cutoff = -self.val_size - self.test_size
                temporal = temporal[:, :, :cutoff]

            temporal = self.padder(temporal)
            windows = temporal.unfold(dimension=-1, 
                                      size=window_size, 
                                      step=self.step_size)

            # [B, C, Ws, L+H] 0, 1, 2, 3
            # -> [B * Ws, L+H, C] 0, 2, 3, 1
            windows_per_serie = windows.shape[2]
            windows = windows.permute(0, 2, 3, 1).contiguous()
            windows = windows.reshape(-1, window_size, len(temporal_cols))

            # Sample and Available conditions
            available_idx = temporal_cols.get_loc('available_mask')
            sample_condition = windows[:, -self.h:, available_idx]
            sample_condition = torch.sum(sample_condition, axis=1)
            available_condition = windows[:, :-self.h, available_idx]
            available_condition = torch.sum(available_condition, axis=1)
            final_condition = (sample_condition > 0) & (available_condition > 0)
            windows = windows[final_condition]

            # Parse Static data to match windows
            # [B, S_in] -> [B, Ws, S_in] -> [B*Ws, S_in]
            static = batch.get('static', None)
            static_cols=batch.get('static_cols', None)
            if static is not None:
                static = torch.repeat_interleave(static, 
                                    repeats=windows_per_serie, dim=0)
                static = static[final_condition]

            # Protection of empty windows
            if final_condition.sum() == 0:
                raise Exception('No windows available for training')

            # Sample windows
            n_windows = len(windows)
            if self.windows_batch_size is not None:
                w_idxs = np.random.choice(n_windows, 
                                          size=self.windows_batch_size,
                                          replace=(n_windows < self.windows_batch_size))
                windows = windows[w_idxs]
                
                if static is not None:
                    static = static[w_idxs]

            # think about interaction available * sample mask
            # [B, C, Ws, L+H]
            windows_batch = dict(temporal=windows,
                                 temporal_cols=temporal_cols,
                                 static=static,
                                 static_cols=static_cols)
            return windows_batch

        elif step in ['predict', 'val']:

            if step == 'predict':
                predict_step_size = self.predict_step_size
                cutoff = - self.input_size - self.test_size
                temporal = batch['temporal'][:, :, cutoff:]

            elif step == 'val':
                predict_step_size = self.step_size
                cutoff = -self.input_size - self.val_size - self.test_size
                if self.test_size > 0:
                    temporal = batch['temporal'][:, :, cutoff:-self.test_size]
                else:
                    temporal = batch['temporal'][:, :, cutoff:]

            if (step=='predict') and (self.test_size==0) and (len(self.futr_exog_list)==0):
               temporal = self.padder(temporal)

            windows = temporal.unfold(dimension=-1,
                                      size=window_size,
                                      step=predict_step_size)

            # [batch, channels, windows, window_size] 0, 1, 2, 3
            # -> [batch * windows, window_size, channels] 0, 2, 3, 1
            windows_per_serie = windows.shape[2]
            windows = windows.permute(0, 2, 3, 1).contiguous()
            windows = windows.reshape(-1, window_size, len(temporal_cols))

            static = batch.get('static', None)
            static_cols=batch.get('static_cols', None)
            if static is not None:
                static = torch.repeat_interleave(static, 
                                    repeats=windows_per_serie, dim=0)

            windows_batch = dict(temporal=windows,
                                 temporal_cols=temporal_cols,
                                 static=static,
                                 static_cols=static_cols)
            return windows_batch
        else:
            raise ValueError(f'Unknown step {step}')
            
    def _normalization(self, windows):
        # windows are already filtered by train/validation/test
        # from the `create_windows_method` nor leakage risk
        temporal = windows['temporal']                  # B, L+H, C
        temporal_cols = windows['temporal_cols'].copy() # B, L+H, C

        # To avoid leakage uses only the lags
        temporal_data_cols = temporal_cols.drop('available_mask').tolist()
        temporal_data = temporal[:, :, temporal_cols.get_indexer(temporal_data_cols)]
        temporal_mask = temporal[:, :, temporal_cols.get_loc('available_mask')].clone()
        temporal_mask[:, -self.h:] = 0.0

        # Normalize. self.scaler stores the shift and scale for inverse transform
        temporal_mask = temporal_mask.unsqueeze(-1) # Add channel dimension for scaler.transform.
        temporal_data = self.scaler.transform(x=temporal_data, mask=temporal_mask)

        # Replace values in windows dict
        temporal[:, :, temporal_cols.get_indexer(temporal_data_cols)] = temporal_data
        windows['temporal'] = temporal

        return windows

    def _inv_normalization(self, y_hat, temporal_cols):
        # Receives window predictions [B, H, output]
        # Broadcasts outputs and inverts normalization

        # Add C dimension
        if y_hat.ndim == 2:
            remove_dimension = True
            y_hat = y_hat.unsqueeze(-1)
        else:
            remove_dimension = False

        temporal_data_cols = temporal_cols.drop('available_mask')
        y_scale = self.scaler.x_scale[:,:,temporal_data_cols.get_indexer(['y'])]
        y_shift = self.scaler.x_shift[:,:,temporal_data_cols.get_indexer(['y'])]

        y_scale = torch.repeat_interleave(y_scale, repeats=y_hat.shape[-1], dim=-1)
        y_shift = torch.repeat_interleave(y_shift, repeats=y_hat.shape[-1], dim=-1)

        y_hat = self.scaler.inverse_transform(z=y_hat, x_scale=y_scale, x_shift=y_shift)

        if remove_dimension:
            y_hat = y_hat.squeeze(-1)

        return y_hat

    def _parse_windows(self, batch, windows):
        # Filter insample lags from outsample horizon
        y_idx = batch['temporal_cols'].get_loc('y')
        mask_idx = batch['temporal_cols'].get_loc('available_mask')
        insample_y = windows['temporal'][:, :-self.h, y_idx]
        insample_mask = windows['temporal'][:, :-self.h, mask_idx]
        outsample_y = windows['temporal'][:, -self.h:, y_idx]
        outsample_mask = windows['temporal'][:, -self.h:, mask_idx]

        # Filter historic exogenous variables
        if len(self.hist_exog_list):
            hist_exog_idx = windows['temporal_cols'].get_indexer(self.hist_exog_list)
            hist_exog = windows['temporal'][:, :-self.h, hist_exog_idx]
        else:
            hist_exog = None
        
        # Filter future exogenous variables
        if len(self.futr_exog_list):
            futr_exog_idx = windows['temporal_cols'].get_indexer(self.futr_exog_list)
            futr_exog = windows['temporal'][:, :, futr_exog_idx]
        else:
            futr_exog = None
        # Filter static variables
        if len(self.stat_exog_list):
            static_idx = windows['static_cols'].get_indexer(self.stat_exog_list)
            stat_exog = windows['static'][:, static_idx]
        else:
            stat_exog = None

        return insample_y, insample_mask, outsample_y, outsample_mask, \
               hist_exog, futr_exog, stat_exog

    def training_step(self, batch, batch_idx):        
        # Create windows [Ws, L+H, C]
        windows = self._create_windows(batch, step='train')
        
        # Normalize windows
        if self.scaler is not None:
            windows = self._normalization(windows=windows)

        # Parse windows
        insample_y, insample_mask, outsample_y, outsample_mask, \
               hist_exog, futr_exog, stat_exog = self._parse_windows(batch, windows)

        windows_batch = dict(insample_y=insample_y, # [Ws, L]
                             insample_mask=insample_mask, # [Ws, L]
                             futr_exog=futr_exog, # [Ws, L+H]
                             hist_exog=hist_exog, # [Ws, L]
                             stat_exog=stat_exog) # [Ws, 1]

        output = self(windows_batch)

        # Possibility of distribution_outputs
        if self.loss.is_distribution_output:
            loss = self.loss(y=outsample_y,
                             distr_args=output,
                             loc=None,
                             scale=None,
                             mask=outsample_mask)
        else:
            loss = self.loss(y=outsample_y, y_hat=output[0], mask=outsample_mask)

        self.log('train_loss', loss, prog_bar=True, on_epoch=True)
        return loss

    def validation_step(self, batch, batch_idx):
        if self.val_size == 0:
            return np.nan
        
        # Create windows [Ws, L+H, C]
        windows = self._create_windows(batch, step='val')
        
        # Normalize windows
        if self.scaler is not None:
            windows = self._normalization(windows=windows)

        # Parse windows
        insample_y, insample_mask, outsample_y, outsample_mask, \
               hist_exog, futr_exog, stat_exog = self._parse_windows(batch, windows)

        windows_batch = dict(insample_y=insample_y, # [Ws, L]
                             insample_mask=insample_mask, # [Ws, L]
                             futr_exog=futr_exog, # [Ws, L+H]
                             hist_exog=hist_exog, # [Ws, L]
                             stat_exog=stat_exog) # [Ws, 1]

        output = self(windows_batch)

        # Possibility of distribution_outputs
        if self.loss.is_distribution_output:
            loss = self.loss(y=outsample_y,
                             distr_args=output,
                             loc=None,
                             scale=None,
                             mask=outsample_mask)
        else:
            loss = self.loss(y=outsample_y, y_hat=output[0], mask=outsample_mask)

        self.log('val_loss', loss, prog_bar=True, on_epoch=True)
        return loss
    
    def validation_epoch_end(self, outputs):
        if self.val_size == 0:
            return
        avg_loss = torch.stack(outputs).mean()
        self.log("ptl/val_loss", avg_loss)
    
    def predict_step(self, batch, batch_idx):        
        # Create windows [Ws, L+H, C]
        windows = self._create_windows(batch, step='predict')

        # Normalize windows
        if self.scaler is not None:
            windows = self._normalization(windows=windows)

        # Parse windows
        insample_y, insample_mask, _, _, \
               hist_exog, futr_exog, stat_exog = self._parse_windows(batch, windows)

        windows_batch = dict(insample_y=insample_y, # [Ws, L]
                             insample_mask=insample_mask, # [Ws, L]
                             futr_exog=futr_exog, # [Ws, L+H]
                             hist_exog=hist_exog, # [Ws, L]
                             stat_exog=stat_exog) # [Ws, 1]

        output = self(windows_batch)

        # Obtain empirical quantiles
        if self.loss.is_distribution_output:
            _, y_hat = self.loss.sample(distr_args=output,
                                        loc=None,
                                        scale=None,
                                        num_samples=500)
        # Parse tuple's first entry
        else:
            y_hat = output[0]

        # Inv Normalize
        if self.scaler is not None:
            y_hat = self._inv_normalization(y_hat=y_hat,
                                            temporal_cols=batch['temporal_cols'])
        return y_hat
    
    def fit(self, dataset, val_size=0, test_size=0):
        """ Fit.

        The `fit` method, optimizes the neural network's weights using the
        initialization parameters (`learning_rate`, `windows_batch_size`, ...)
        and the `loss` function as defined during the initialization. 
        Within `fit` we use a PyTorch Lightning `Trainer` that
        inherits the initialization's `self.trainer_kwargs`, to customize
        its inputs, see [PL's trainer arguments](https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.trainer.trainer.Trainer.html?highlight=trainer).

        The method is designed to be compatible with SKLearn-like classes
        and in particular to be compatible with the StatsForecast library.

        By default the `model` is not saving training checkpoints to protect 
        disk memory, to get them change `enable_checkpointing=True` in `__init__`.

        **Parameters:**<br>
        `dataset`: NeuralForecast's `TimeSeriesDataset`, see [documentation](https://nixtla.github.io/neuralforecast/tsdataset.html).<br>
        `val_size`: int, validation size for temporal cross-validation.<br>
        `test_size`: int, test size for temporal cross-validation.<br>
        """
        self.val_size = val_size
        self.test_size = test_size
        datamodule = TimeSeriesDataModule(
            dataset, 
            batch_size=self.batch_size,
            num_workers=self.num_workers_loader,
            drop_last=self.drop_last_loader
        )

        trainer = pl.Trainer(**self.trainer_kwargs)
        trainer.fit(self, datamodule=datamodule)

    def predict(self, dataset, test_size=None, step_size=1, **data_module_kwargs):
        """ Predict.

        Neural network prediction with PL's `Trainer` execution of `predict_step`.

        **Parameters:**<br>
        `dataset`: NeuralForecast's `TimeSeriesDataset`, see [documentation](https://nixtla.github.io/neuralforecast/tsdataset.html).<br>
        `test_size`: int=None, test size for temporal cross-validation.<br>
        `step_size`: int=1, Step size between each window.<br>
        `**data_module_kwargs`: PL's TimeSeriesDataModule args, see [documentation](https://pytorch-lightning.readthedocs.io/en/1.6.1/extensions/datamodules.html#using-a-datamodule).
        """
        self.predict_step_size = step_size
        self.decompose_forecast = False
        datamodule = TimeSeriesDataModule(dataset, **data_module_kwargs)
        trainer = pl.Trainer(**self.trainer_kwargs)
        fcsts = trainer.predict(self, datamodule=datamodule)        
        fcsts = torch.vstack(fcsts).numpy().flatten()    
        fcsts = fcsts.reshape(-1, len(self.loss.output_names))
        return fcsts

    def decompose(self, dataset, step_size=1, **data_module_kwargs):
        """ Decompose Predictions.

        Decompose the predictions through the network's layers.
        Available methods are `ESRNN`, `NHITS`, `NBEATS`, and `NBEATSx`.

        **Parameters:**<br>
        `dataset`: NeuralForecast's `TimeSeriesDataset`, see [documentation here](https://nixtla.github.io/neuralforecast/tsdataset.html).<br>
        `step_size`: int=1, step size between each window of temporal data.<br>
        `**data_module_kwargs`: PL's TimeSeriesDataModule args, see [documentation](https://pytorch-lightning.readthedocs.io/en/1.6.1/extensions/datamodules.html#using-a-datamodule).
        """
        self.predict_step_size = step_size
        self.decompose_forecast = True
        datamodule = TimeSeriesDataModule(dataset, **data_module_kwargs)
        trainer = pl.Trainer(**self.trainer_kwargs)
        fcsts = trainer.predict(self, datamodule=datamodule)
        self.decompose_forecast = False # Default decomposition back to false
        return torch.vstack(fcsts).numpy()

    def forward(self, insample_y, insample_mask):
        raise NotImplementedError('forward')

    def set_test_size(self, test_size):
        self.test_size = test_size

    def save(self, path):
        """ BaseWindows.save

        Save the fitted model to disk.

        **Parameters:**<br>
        `path`: str, path to save the model.<br>
        """
        self.trainer.save_checkpoint(path)
