"""
Regression Model
----------------
A `RegressionModel` forecasts future values of a target series based on lagged values of

* The target series (past lags only)

* An optional past_covariates series (past lags only)

* An optional future_covariates series (possibly past and future lags)


The regression models are learned in a supervised way, and they can wrap around any "scikit-learn like" regression model
acting on tabular data having ``fit()`` and ``predict()`` methods.

Darts also provides :class:`LinearRegressionModel` and :class:`RandomForest`, which are regression models
wrapping around scikit-learn linear regression and random forest regression, respectively.

Behind the scenes this model is tabularizing the time series data to make it work with regression models.

The lags can be specified either using an integer - in which case it represents the _number_ of (past or future) lags
to take into consideration, or as a list - in which case the lags have to be enumerated (strictly negative values
denoting past lags and positive values including 0 denoting future lags).
"""

import math
from collections import OrderedDict
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.linear_model import LinearRegression

from darts.logging import get_logger, raise_if, raise_if_not, raise_log
from darts.models.forecasting.forecasting_model import GlobalForecastingModel
from darts.timeseries import TimeSeries
from darts.utils.multioutput import MultiOutputRegressor
from darts.utils.utils import _check_quantiles, seq2series, series2seq

logger = get_logger(__name__)


class RegressionModel(GlobalForecastingModel):
    def __init__(
        self,
        lags: Union[int, list] = None,
        lags_past_covariates: Union[int, List[int]] = None,
        lags_future_covariates: Union[Tuple[int, int], List[int]] = None,
        output_chunk_length: int = 1,
        add_encoders: Optional[dict] = None,
        model=None,
    ):
        """Regression Model
        Can be used to fit any scikit-learn-like regressor class to predict the target time series from lagged values.

        Parameters
        ----------
        lags
            Lagged target values used to predict the next time step. If an integer is given the last `lags` past lags
            are used (from -1 backward). Otherwise a list of integers with lags is required (each lag must be < 0).
        lags_past_covariates
            Number of lagged past_covariates values used to predict the next time step. If an integer is given the last
            `lags_past_covariates` past lags are used (inclusive, starting from lag -1). Otherwise a list of integers
            with lags < 0 is required.
        lags_future_covariates
            Number of lagged future_covariates values used to predict the next time step. If an tuple (past, future) is
            given the last `past` lags in the past are used (inclusive, starting from lag -1) along with the first
            `future` future lags (starting from 0 - the prediction time - up to `future - 1` included). Otherwise a list
            of integers with lags is required.
        output_chunk_length
            Number of time steps predicted at once by the internal regression model. Does not have to equal the forecast
            horizon `n` used in `predict()`. However, setting `output_chunk_length` equal to the forecast horizon may
            be useful if the covariates don't extend far enough into the future.
        add_encoders
            A large number of past and future covariates can be automatically generated with `add_encoders`.
            This can be done by adding multiple pre-defined index encoders and/or custom user-made functions that
            will be used as index encoders. Additionally, a transformer such as Darts' :class:`Scaler` can be added to
            transform the generated covariates. This happens all under one hood and only needs to be specified at
            model creation.
            Read :meth:`SequentialEncoder <darts.utils.data.encoders.SequentialEncoder>` to find out more about
            ``add_encoders``. Default: ``None``. An example showing some of ``add_encoders`` features:

            .. highlight:: python
            .. code-block:: python

                add_encoders={
                    'cyclic': {'future': ['month']},
                    'datetime_attribute': {'future': ['hour', 'dayofweek']},
                    'position': {'past': ['absolute'], 'future': ['relative']},
                    'custom': {'past': [lambda idx: (idx.year - 1950) / 50]},
                    'transformer': Scaler()
                }
            ..
        model
            Scikit-learn-like model with ``fit()`` and ``predict()`` methods. Also possible to use model that doesn't
            support multi-output regression for multivariate timeseries, in which case one regressor
            will be used per component in the multivariate series.
            If None, defaults to: ``sklearn.linear_model.LinearRegression(n_jobs=-1)``.
        """

        super().__init__(add_encoders=add_encoders)

        self.model = model
        self.lags = {}
        self.output_chunk_length = None
        self.input_dim = None

        # model checks
        if self.model is None:
            self.model = LinearRegression(n_jobs=-1)

        if not callable(getattr(self.model, "fit", None)):
            raise_log(
                Exception("Provided model object must have a fit() method", logger)
            )
        if not callable(getattr(self.model, "predict", None)):
            raise_log(
                Exception("Provided model object must have a predict() method", logger)
            )

        # check lags
        raise_if(
            (lags is None)
            and (lags_future_covariates is None)
            and (lags_past_covariates is None),
            "At least one of `lags`, `lags_future_covariates` or `lags_past_covariates` must be not None.",
        )

        lags_type_checks = [
            (lags, "lags"),
            (lags_past_covariates, "lags_past_covariates"),
        ]

        for _lags, lags_name in lags_type_checks:
            raise_if_not(
                isinstance(_lags, (int, list)) or _lags is None,
                f"`{lags_name}` must be of type int or list. Given: {type(_lags)}.",
            )
            raise_if(
                isinstance(_lags, bool),
                f"`{lags_name}` must be of type int or list, not bool.",
            )

        raise_if_not(
            isinstance(lags_future_covariates, (tuple, list))
            or lags_future_covariates is None,
            f"`lags_future_covariates` must be of type tuple or list. Given: {type(lags_future_covariates)}.",
        )

        if isinstance(lags_future_covariates, tuple):
            raise_if_not(
                len(lags_future_covariates) == 2
                and isinstance(lags_future_covariates[0], int)
                and isinstance(lags_future_covariates[1], int),
                "`lags_future_covariates` tuple must be of length 2, and must contain two integers",
            )
            raise_if(
                isinstance(lags_future_covariates[0], bool)
                or isinstance(lags_future_covariates[1], bool),
                "`lags_future_covariates` tuple must contain integers, not bool",
            )

        # set lags
        if isinstance(lags, int):
            raise_if_not(lags > 0, f"`lags` must be strictly positive. Given: {lags}.")
            # selecting last `lags` lags, starting from position 1 (skipping current, pos 0, the one we want to predict)
            self.lags["target"] = list(range(-lags, 0))
        elif isinstance(lags, list):
            for lag in lags:
                raise_if(
                    not isinstance(lag, int) or (lag >= 0),
                    f"Every element of `lags` must be a strictly negative integer. Given: {lags}.",
                )
            if lags:
                self.lags["target"] = sorted(lags)

        if isinstance(lags_past_covariates, int):
            raise_if_not(
                lags_past_covariates > 0,
                f"`lags_past_covariates` must be an integer > 0. Given: {lags_past_covariates}.",
            )
            self.lags["past"] = list(range(-lags_past_covariates, 0))
        elif isinstance(lags_past_covariates, list):
            for lag in lags_past_covariates:
                raise_if(
                    not isinstance(lag, int) or (lag >= 0),
                    f"Every element of `lags_covariates` must be an integer < 0. Given: {lags_past_covariates}.",
                )
            if lags_past_covariates:
                self.lags["past"] = sorted(lags_past_covariates)

        if isinstance(lags_future_covariates, tuple):
            raise_if_not(
                lags_future_covariates[0] >= 0 and lags_future_covariates[1] >= 0,
                f"`lags_future_covariates` tuple must contain integers >= 0. Given: {lags_future_covariates}.",
            )
            if (
                lags_future_covariates[0] is not None
                and lags_future_covariates[1] is not None
            ):
                if not (
                    lags_future_covariates[0] == 0 and lags_future_covariates[1] == 0
                ):
                    self.lags["future"] = list(
                        range(-lags_future_covariates[0], lags_future_covariates[1])
                    )
        elif isinstance(lags_future_covariates, list):
            for lag in lags_future_covariates:
                raise_if(
                    not isinstance(lag, int) or isinstance(lag, bool),
                    f"Every element of `lags_future_covariates` must be an integer. Given: {lags_future_covariates}.",
                )
            if lags_future_covariates:
                self.lags["future"] = sorted(lags_future_covariates)

        # check and set output_chunk_length
        raise_if_not(
            isinstance(output_chunk_length, int) and output_chunk_length > 0,
            f"output_chunk_length must be an integer greater than 0. Given: {output_chunk_length}",
        )
        self.output_chunk_length = output_chunk_length

    @property
    def _model_encoder_settings(self) -> Tuple[int, int, bool, bool]:
        lags_covariates = {
            lag for key in ["past", "future"] for lag in self.lags.get(key, [])
        }
        if lags_covariates:
            # for lags < 0 we need to take `n` steps backwards from past and/or historic future covariates
            # for minimum lag = -1 -> steps_back_inclusive = 1
            # inclusive means n steps back including the end of the target series
            n_steps_back_inclusive = abs(min(min(lags_covariates), 0))
            # for lags >= 0 we need to take `n` steps ahead from future covariates
            # for maximum lag = 0 -> output_chunk_length = 1
            # exclusive means n steps ahead after the last step of the target series
            n_steps_ahead_exclusive = max(max(lags_covariates), 0) + 1
            takes_past_covariates = "past" in self.lags
            takes_future_covariates = "future" in self.lags
        else:
            n_steps_back_inclusive = 0
            n_steps_ahead_exclusive = 0
            takes_past_covariates = False
            takes_future_covariates = False
        return (
            n_steps_back_inclusive,
            n_steps_ahead_exclusive,
            takes_past_covariates,
            takes_future_covariates,
        )

    def _get_encoders_n(self, n) -> int:
        """Returns the `n` encoder prediction steps specific to RegressionModels.
        This will generate slightly more past covariates than the minimum requirement when using past and future
        covariate lags simultaneously. This is because encoders were written for TorchForecastingModels where we only
        needed `n` future covariates. For RegressionModel we need `n + max_future_lag`
        """
        _, n_steps_ahead, _, takes_future_covariates = self._model_encoder_settings
        if not takes_future_covariates:
            return n
        else:
            return n + (n_steps_ahead - 1)

    @property
    def min_train_series_length(self) -> int:
        return max(
            3,
            -self.lags["target"][0] + self.output_chunk_length
            if "target" in self.lags
            else self.output_chunk_length,
        )

    def _get_last_prediction_time(self, series, forecast_horizon, overlap_end):
        # overrides the ForecastingModel _get_last_prediction_time, taking care of future lags if any
        extra_shift = max(0, max(lags[-1] for lags in self.lags.values()))

        if overlap_end:
            last_valid_pred_time = series.time_index[-1 - extra_shift]
        else:
            last_valid_pred_time = series.time_index[-forecast_horizon - extra_shift]

        return last_valid_pred_time

    def _create_lagged_data(
        self, target_series, past_covariates, future_covariates, max_samples_per_ts
    ):
        """
        Helper function that creates training/validation matrices (X and y as required in sklearn), given series and
        max_samples_per_ts.

        X has the following structure:
        lags_target | lags_past_covariates | lags_future_covariates

        Where each lags_X has the following structure (lags_X=[-2,-1] and X has 2 components):
        lag_-2_comp_1_X | lag_-2_comp_2_X | lag_-1_comp_1_X | lag_-1_comp_2_X

        y has the following structure (output_chunk_length=4 and target has 2 components):
        lag_+0_comp_1_target | lag_+0_comp_2_target | ... | lag_+3_comp_1_target | lag_+3_comp_2_target
        """

        # ensure list of TimeSeries format
        if isinstance(target_series, TimeSeries):
            target_series = [target_series]
            past_covariates = [past_covariates] if past_covariates else None
            future_covariates = [future_covariates] if future_covariates else None

        Xs, ys = [], []
        # iterate over series
        for idx, target_ts in enumerate(target_series):
            covariates = [
                (
                    past_covariates[idx].pd_dataframe(copy=False)
                    if past_covariates
                    else None,
                    self.lags.get("past"),
                ),
                (
                    future_covariates[idx].pd_dataframe(copy=False)
                    if future_covariates
                    else None,
                    self.lags.get("future"),
                ),
            ]

            df_X = []
            df_y = []
            df_target = target_ts.pd_dataframe(copy=False)

            # y: output chunk length lags of target
            for future_target_lag in range(self.output_chunk_length):
                df_y.append(df_target.shift(-future_target_lag))

            # X: target lags
            if "target" in self.lags:
                for lag in self.lags["target"]:
                    df_X.append(df_target.shift(-lag))

            # X: covariate lags
            for df_cov, lags in covariates:
                if lags:
                    for lag in lags:
                        df_X.append(df_cov.shift(-lag))

            # combine lags
            df_X = pd.concat(df_X, axis=1)
            df_y = pd.concat(df_y, axis=1)
            df_X_y = pd.concat([df_X, df_y], axis=1)
            X_y = df_X_y.dropna().values

            # keep most recent max_samples_per_ts samples
            if max_samples_per_ts:
                X_y = X_y[-max_samples_per_ts:]

            raise_if(
                X_y.shape[0] == 0,
                "Unable to build any training samples of the target series "
                + (f"at index {idx} " if len(target_series) > 1 else "")
                + "and the corresponding covariate series; "
                "There is no time step for which all required lags are available and are not NaN values.",
            )

            X, y = np.split(X_y, [df_X.shape[1]], axis=1)
            Xs.append(X)
            ys.append(y)

        # combine samples from all series
        X = np.concatenate(Xs, axis=0)
        y = np.concatenate(ys, axis=0)
        return X, y

    def _fit_model(
        self,
        target_series,
        past_covariates,
        future_covariates,
        max_samples_per_ts,
        **kwargs,
    ):
        """
        Function that fit the model. Deriving classes can override this method for adding additional parameters (e.g.,
        adding validation data), keeping the sanity checks on series performed by fit().
        """

        training_samples, training_labels = self._create_lagged_data(
            target_series, past_covariates, future_covariates, max_samples_per_ts
        )

        # if training_labels is of shape (n_samples, 1) flatten it to shape (n_samples,)
        if len(training_labels.shape) == 2 and training_labels.shape[1] == 1:
            training_labels = training_labels.ravel()
        self.model.fit(training_samples, training_labels, **kwargs)

    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        max_samples_per_ts: Optional[int] = None,
        n_jobs_multioutput_wrapper: Optional[int] = None,
        **kwargs,
    ):
        """
        Fit/train the model on one or multiple series.

        Parameters
        ----------
        series
            TimeSeries or Sequence[TimeSeries] object containing the target values.
        past_covariates
            Optionally, a series or sequence of series specifying past-observed covariates
        future_covariates
            Optionally, a series or sequence of series specifying future-known covariates
        max_samples_per_ts
            This is an integer upper bound on the number of tuples that can be produced
            per time series. It can be used in order to have an upper bound on the total size of the dataset and
            ensure proper sampling. If `None`, it will read all of the individual time series in advance (at dataset
            creation) to know their sizes, which might be expensive on big datasets.
            If some series turn out to have a length that would allow more than `max_samples_per_ts`, only the
            most recent `max_samples_per_ts` samples will be considered.
        n_jobs_multioutput_wrapper
            Number of jobs of the MultiOutputRegressor wrapper to run in parallel. Only used if the model doesn't
            support multi-output regression natively.
        **kwargs
            Additional keyword arguments passed to the `fit` method of the model.
        """
        # guarantee that all inputs are either list of TimeSeries or None
        series = series2seq(series)
        past_covariates = series2seq(past_covariates)
        future_covariates = series2seq(future_covariates)

        self.encoders = self.initialize_encoders()
        if self.encoders.encoding_available:
            past_covariates, future_covariates = self.generate_fit_encodings(
                series=series,
                past_covariates=past_covariates,
                future_covariates=future_covariates,
            )

        for covs, name in zip([past_covariates, future_covariates], ["past", "future"]):
            raise_if(
                covs is not None and name not in self.lags,
                f"`{name}_covariates` not None in `fit()` method call, but `lags_{name}_covariates` is None in "
                f"constructor.",
            )

            raise_if(
                covs is None and name in self.lags,
                f"`{name}_covariates` is None in `fit()` method call, but `lags_{name}_covariates` is not None in "
                "constructor.",
            )

        # saving the dims of all input series to check at prediction time
        self.input_dim = {
            "target": series[0].width,
            "past": past_covariates[0].width if past_covariates else None,
            "future": future_covariates[0].width if future_covariates else None,
        }

        # if multi-output regression
        if not series[0].is_univariate or self.output_chunk_length > 1:
            # and model isn't wrapped already
            if not isinstance(self.model, MultiOutputRegressor):
                # check whether model supports multi-output regression natively
                if not (
                    callable(getattr(self.model, "_get_tags", None))
                    and isinstance(self.model._get_tags(), dict)
                    and self.model._get_tags().get("multioutput")
                ):
                    # if not, wrap model with MultiOutputRegressor
                    self.model = MultiOutputRegressor(
                        self.model, n_jobs=n_jobs_multioutput_wrapper
                    )
                elif isinstance(self.model, CatBoostRegressor):
                    if (
                        self.model.get_params()["loss_function"]
                        == "RMSEWithUncertainty"
                    ):
                        self.model = MultiOutputRegressor(
                            self.model, n_jobs=n_jobs_multioutput_wrapper
                        )

        # warn if n_jobs_multioutput_wrapper was provided but not used
        if (
            not isinstance(self.model, MultiOutputRegressor)
            and n_jobs_multioutput_wrapper is not None
        ):
            logger.warning("Provided `n_jobs_multioutput_wrapper` wasn't used.")

        super().fit(
            series=seq2series(series),
            past_covariates=seq2series(past_covariates),
            future_covariates=seq2series(future_covariates),
        )

        self._fit_model(
            series, past_covariates, future_covariates, max_samples_per_ts, **kwargs
        )

        return self

    def predict(
        self,
        n: int,
        series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        num_samples: int = 1,
        **kwargs,
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Forecasts values for `n` time steps after the end of the series.

        Parameters
        ----------
        n : int
            Forecast horizon - the number of time steps after the end of the series for which to produce predictions.
        series : TimeSeries or list of TimeSeries, optional
            Optionally, one or several input `TimeSeries`, representing the history of the target series whose future
            is to be predicted. If specified, the method returns the forecasts of these series. Otherwise, the method
            returns the forecast of the (single) training series.
        past_covariates : TimeSeries or list of TimeSeries, optional
            Optionally, the past-observed covariates series needed as inputs for the model.
            They must match the covariates used for training in terms of dimension and type.
        future_covariates : TimeSeries or list of TimeSeries, optional
            Optionally, the future-known covariates series needed as inputs for the model.
            They must match the covariates used for training in terms of dimension and type.
        num_samples : int, default: 1
            Currently this parameter is ignored for regression models.
        **kwargs : dict, optional
            Additional keyword arguments passed to the `predict` method of the model. Only works with
            univariate target series.
        """
        raise_if(
            not self._is_probabilistic() and num_samples > 1,
            "`num_samples > 1` is only supported for probabilistic models.",
            logger,
        )

        if series is None:
            # then there must be a single TS, and that was saved in super().fit as self.training_series
            raise_if(
                self.training_series is None,
                "Input series has to be provided after fitting on multiple series.",
            )
            series = self.training_series

        called_with_single_series = True if isinstance(series, TimeSeries) else False

        # guarantee that all inputs are either list of TimeSeries or None
        series = series2seq(series)
        past_covariates = series2seq(past_covariates)
        future_covariates = series2seq(future_covariates)

        if self.encoders.encoding_available:
            past_covariates, future_covariates = self.generate_predict_encodings(
                n=n,
                series=series,
                past_covariates=past_covariates,
                future_covariates=future_covariates,
            )

        if past_covariates is None and self.past_covariate_series is not None:
            past_covariates = series2seq(self.past_covariate_series)
        if future_covariates is None and self.future_covariate_series is not None:
            future_covariates = series2seq(self.future_covariate_series)

        super().predict(n, series, past_covariates, future_covariates, num_samples)

        # check that the input sizes of the target series and covariates match
        pred_input_dim = {
            "target": series[0].width,
            "past": past_covariates[0].width if past_covariates else None,
            "future": future_covariates[0].width if future_covariates else None,
        }
        raise_if_not(
            pred_input_dim == self.input_dim,
            f"The number of components of the target series and the covariates provided for prediction doesn't "
            f"match the number of components of the target series and the covariates this model has been "
            f"trained on.\n"
            f"Provided number of components for prediction: {pred_input_dim}\n"
            f"Provided number of components for training: {self.input_dim}",
        )

        # prediction preprocessing

        covariates = {
            "past": (past_covariates, self.lags.get("past")),
            "future": (future_covariates, self.lags.get("future")),
        }

        # dictionary containing covariate data over time span required for prediction
        covariate_matrices = {}
        # dictionary containing covariate lags relative to minimum covariate lag
        relative_cov_lags = {}
        # number of prediction steps given forecast horizon and output_chunk_length
        n_pred_steps = math.ceil(n / self.output_chunk_length)
        for cov_type, (covs, lags) in covariates.items():
            if covs is not None:
                relative_cov_lags[cov_type] = np.array(lags) - lags[0]
                covariate_matrices[cov_type] = []
                for idx, (ts, cov) in enumerate(zip(series, covs)):
                    # calculating first and last prediction time steps
                    first_pred_ts = ts.end_time() + 1 * ts.freq
                    last_pred_ts = (
                        first_pred_ts
                        + ((n_pred_steps - 1) * self.output_chunk_length) * ts.freq
                    )
                    # calculating first and last required time steps
                    first_req_ts = first_pred_ts + lags[0] * ts.freq
                    last_req_ts = last_pred_ts + lags[-1] * ts.freq

                    # check for sufficient covariate data
                    raise_if_not(
                        cov.start_time() <= first_req_ts
                        and cov.end_time() >= last_req_ts,
                        f"The corresponding {cov_type}_covariate of the series at index {idx} isn't sufficiently long. "
                        f"Given horizon `n={n}`, `min(lags_{cov_type}_covariates)={lags[0]}`, "
                        f"`max(lags_{cov_type}_covariates)={lags[-1]}` and "
                        f"`output_chunk_length={self.output_chunk_length}`\n"
                        f"the {cov_type}_covariate has to range from {first_req_ts} until {last_req_ts} (inclusive), "
                        f"but it ranges only from {cov.start_time()} until {cov.end_time()}.",
                    )

                    # Note: we use slice() rather than the [] operator because
                    # for integer-indexed series [] does not act on the time index.
                    last_req_ts = (
                        # For range indexes, we need to make the end timestamp inclusive here
                        last_req_ts + ts.freq
                        if ts.has_range_index
                        else last_req_ts
                    )
                    covariate_matrices[cov_type].append(
                        cov.slice(first_req_ts, last_req_ts).values(copy=False)
                    )

                covariate_matrices[cov_type] = np.stack(covariate_matrices[cov_type])

        series_matrix = None
        if "target" in self.lags:
            series_matrix = np.stack(
                [ts[self.lags["target"][0] :].values(copy=False) for ts in series]
            )

        # repeat series_matrix to shape (num_samples * num_series, n_lags, n_components)
        # [series 0 sample 0, series 0 sample 1, ..., series n sample k]
        series_matrix = np.repeat(series_matrix, num_samples, axis=0)

        # same for covariate matrices
        for cov_type, data in covariate_matrices.items():
            covariate_matrices[cov_type] = np.repeat(data, num_samples, axis=0)
        # prediction
        predictions = []
        # t_pred indicates the number of time steps after the first prediction
        for t_pred in range(0, n, self.output_chunk_length):
            np_X = []
            # retrieve target lags
            if "target" in self.lags:

                target_matrix = (
                    np.concatenate([series_matrix, *predictions], axis=1)
                    if predictions
                    else series_matrix
                )
                np_X.append(
                    target_matrix[:, self.lags["target"]].reshape(
                        len(series) * num_samples, -1
                    )
                )
            # retrieve covariate lags, enforce order (dict only preserves insertion order for python 3.6+)
            for cov_type in ["past", "future"]:
                if cov_type in covariate_matrices:
                    np_X.append(
                        covariate_matrices[cov_type][
                            :, relative_cov_lags[cov_type] + t_pred
                        ].reshape(len(series) * num_samples, -1)
                    )

            # concatenate retrieved lags
            X = np.concatenate(np_X, axis=1)
            # X has shape (n_series * n_samples, n_regression_features)
            prediction = self._predict_and_sample(X, num_samples, **kwargs)
            # prediction shape (n_series * n_samples, output_chunk_length, n_components)
            # append prediction to final predictions
            predictions.append(prediction)

        # concatenate and use first n points as prediction
        predictions = np.concatenate(predictions, axis=1)[:, :n]

        # bring into correct shape: (n_series, output_chunk_length, n_components, n_samples)
        predictions = np.moveaxis(
            predictions.reshape(len(series), num_samples, n, -1), 1, -1
        )
        # build time series from the predicted values starting after end of series
        predictions = [
            self._build_forecast_series(row, input_tgt)
            for row, input_tgt in zip(predictions, series)
        ]

        return predictions[0] if called_with_single_series else predictions

    def _predict_and_sample(
        self, x: np.ndarray, num_samples: int, **kwargs
    ) -> np.ndarray:
        prediction = self.model.predict(x, **kwargs)
        k = x.shape[0]
        return prediction.reshape(k, self.output_chunk_length, -1)

    def __str__(self):
        return self.model.__str__()


class _LikelihoodMixin:
    """
    A class containing functions supporting quantile, poisson and gaussian regression, to be used as a mixin for some
    `RegressionModel` subclasses.
    """

    @staticmethod
    def _check_likelihood(likelihood, available_likelihoods):
        raise_if_not(
            likelihood in available_likelihoods,
            f"If likelihood is specified it must be one of {available_likelihoods}",
        )

    @staticmethod
    def _get_model_container():
        return _QuantileModelContainer()

    @staticmethod
    def _prepare_quantiles(quantiles):
        if quantiles is None:
            quantiles = [
                0.01,
                0.05,
                0.1,
                0.25,
                0.5,
                0.75,
                0.9,
                0.95,
                0.99,
            ]
        else:
            quantiles = sorted(quantiles)
            _check_quantiles(quantiles)
        median_idx = quantiles.index(0.5)

        return quantiles, median_idx

    def _predict_quantiles(
        self, x: np.ndarray, num_samples: int, **kwargs
    ) -> np.ndarray:
        """
        X is of shape (n_series * n_samples, n_regression_features)
        """
        k = x.shape[0]
        if num_samples == 1:
            # return median
            fitted = self._model_container[0.5]
            return fitted.predict(x, **kwargs).reshape(k, self.output_chunk_length, -1)

        model_outputs = []
        for quantile, fitted in self._model_container.items():
            self.model = fitted
            # model output has shape (n_series * n_samples, output_chunk_length, n_components)
            model_output = fitted.predict(x, **kwargs).reshape(
                k, self.output_chunk_length, -1
            )
            model_outputs.append(model_output)
        model_outputs = np.stack(model_outputs, axis=-1)
        # model_outputs has shape (n_series * n_samples, output_chunk_length, n_components, n_quantiles)

        sampled = self._quantile_sampling(model_outputs)

        # sampled has shape (n_series * n_samples, output_chunk_length, n_components)

        return sampled

    def _predict_normal(self, x: np.ndarray, num_samples: int, **kwargs) -> np.ndarray:
        """Method intended for CatBoost's RMSEWithUncertainty loss. Returns samples
        computed from double-valued inputs [mean, variance].
        X is of shape (n_series * n_samples, n_regression_features)
        """
        k = x.shape[0]

        # model_output shape:
        # if univariate & output_chunk_length = 1: (num_samples, 2)
        # else: (2, num_samples, n_components * output_chunk_length)
        # where the axis with 2 dims is mu, sigma
        model_output = self.model.predict(x, **kwargs)
        output_dim = len(model_output.shape)

        # deterministic case: we return the mean only
        if num_samples == 1:
            # univariate & single-chunk output
            if output_dim <= 2:
                output_slice = model_output[:, 0]
            else:
                output_slice = model_output[0, :, :]

            return output_slice.reshape(k, self.output_chunk_length, -1)

        # probabilistic case
        # univariate & single-chunk output
        if output_dim <= 2:
            # embedding well shaped 2D output into 3D
            model_output = np.expand_dims(model_output, axis=0)

        else:
            # we transpose to get mu, sigma couples on last axis
            # shape becomes: (n_components * output_chunk_length, num_samples, 2)
            model_output = model_output.transpose()

        return self._normal_sampling(model_output, num_samples)

    def _normal_sampling(self, model_output: np.ndarray, n_samples: int) -> np.ndarray:
        """Sampling method for CatBoost's [mean, variance] output.
        model_output is of shape (n_components * output_chunk_length, n_samples, 2),
        where the last 2 dimensions are mu and sigma.
        """
        shape = model_output.shape
        chunk_len = self.output_chunk_length

        # treating each component separately
        mu_sigma_list = [model_output[i, :, :] for i in range(shape[0])]

        list_of_samples = [
            self._rng.normal(
                mu_sigma[:, 0],  # mean vector
                mu_sigma[:, 1],  # diagonal covariance matrix
            )
            for mu_sigma in mu_sigma_list
        ]

        samples_transposed = np.array(list_of_samples).transpose()
        samples_reshaped = samples_transposed.reshape(n_samples, chunk_len, -1)

        return samples_reshaped

    def _predict_poisson(self, x: np.ndarray, num_samples: int, **kwargs) -> np.ndarray:
        """
        X is of shape (n_series * n_samples, n_regression_features)
        """
        k = x.shape[0]

        model_output = self.model.predict(x, **kwargs).reshape(
            k, self.output_chunk_length, -1
        )
        if num_samples == 1:
            return model_output

        return self._poisson_sampling(model_output)

    def _poisson_sampling(self, model_output: np.ndarray) -> np.ndarray:
        """
        Model_output is of shape (n_series * n_samples, output_chunk_length, n_components)
        """

        return self._rng.poisson(lam=model_output).astype(float)

    def _quantile_sampling(self, model_output: np.ndarray) -> np.ndarray:
        """
        Sample uniformly between [0, 1] (for each batch example) and return the linear interpolation between the fitted
        quantiles closest to the sampled value.

        model_output is of shape (batch_size, n_timesteps, n_components, n_quantiles)
        """
        num_samples, n_timesteps, n_components, n_quantiles = model_output.shape

        # obtain samples
        probs = self._rng.uniform(
            size=(
                num_samples,
                n_timesteps,
                n_components,
                1,
            )
        )

        # add dummy dim
        probas = np.expand_dims(probs, axis=-2)

        # tile and transpose
        p = np.tile(probas, (1, 1, 1, n_quantiles, 1)).transpose((0, 1, 2, 4, 3))

        # prepare quantiles
        tquantiles = np.array(self.quantiles).reshape((1, 1, 1, -1))

        # calculate index of the largest quantile smaller than the sampled value
        left_idx = np.sum(p > tquantiles, axis=-1)

        # obtain index of the smallest quantile larger than the sampled value
        right_idx = left_idx + 1

        # repeat the model output on the edges
        repeat_count = [1] * n_quantiles
        repeat_count[0] = 2
        repeat_count[-1] = 2
        repeat_count = np.array(repeat_count)
        shifted_output = np.repeat(model_output, repeat_count, axis=-1)

        # obtain model output values corresponding to the quantiles left and right of the sampled value
        left_value = np.take_along_axis(shifted_output, left_idx, axis=-1)
        right_value = np.take_along_axis(shifted_output, right_idx, axis=-1)

        # add 0 and 1 to quantiles
        ext_quantiles = [0.0] + self.quantiles + [1.0]
        expanded_q = np.tile(np.array(ext_quantiles), left_idx.shape)

        # calculate closest quantiles to the sampled value
        left_q = np.take_along_axis(expanded_q, left_idx, axis=-1)
        right_q = np.take_along_axis(expanded_q, right_idx, axis=-1)

        # linear interpolation
        weights = (probs - left_q) / (right_q - left_q)
        inter = left_value + weights * (right_value - left_value)

        return inter.squeeze(-1)


class _QuantileModelContainer(OrderedDict):
    def __init__(self):
        super().__init__()

    def __str__(self):
        return f"_QuantileModelContainer(quantiles={list(self.keys())})"
