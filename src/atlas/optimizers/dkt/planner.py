#!/usr/bin/env python

import os
import pickle
import sys
import time

import gpytorch
import numpy as np
import olympus
import torch
from botorch.acquisition import (
    ExpectedImprovement,
    qExpectedImprovement,
    qNoisyExpectedImprovement,
)
from botorch.fit import fit_gpytorch_model
from botorch.models.gpytorch import GPyTorchModel
from botorch.optim import (
    optimize_acqf,
    optimize_acqf_discrete,
    optimize_acqf_mixed,
)
from gpytorch.distributions import MultivariateNormal
from gpytorch.models import GP
from olympus import ParameterVector
from olympus.planners import AbstractPlanner, CustomPlanner, Planner
from olympus.scalarizers import Scalarizer

from atlas import Logger
from atlas.networks.dkt.dkt import DKT
from atlas.optimizers.acqfs import (
    FeasibilityAwareEI,
    FeasibilityAwareGeneral,
    FeasibilityAwareQEI,
    create_available_options,
    get_batch_initial_conditions,
)
from atlas.optimizers.acquisition_optimizers.base_optimizer import (
    AcquisitionOptimizer,
)
from atlas.optimizers.base import BasePlanner
from atlas.optimizers.gps import (
    CategoricalSingleTaskGP,
    ClassificationGPMatern,
)
from atlas.optimizers.utils import (
    Scaler,
    cat_param_to_feat,
    flip_source_tasks,
    forward_normalize,
    forward_standardize,

    get_cat_dims,
    get_fixed_features_list,
    infer_problem_type,
    propose_randomly,
    reverse_normalize,
    reverse_standardize,
)


class DKTModel(GP, GPyTorchModel):

    # meta-data for botorch
    _num_outputs = 1

    def __init__(self, model, context_x, context_y):
        super().__init__()
        self.model = model
        self.context_x = context_x.float()
        self.context_y = context_y.float()

    def forward(self, x):
        """
        x shape  (# proposals, q_batch_size, # params)
        mean shape (# proposals, # params)
        covar shape (# proposals, q_batch_size, # params)
        """
        x = x.float()
        _, __, likelihood = self.model.forward(
            self.context_x, self.context_y, x
        )
        mean = likelihood.mean
        covar = likelihood.lazy_covariance_matrix

        return gpytorch.distributions.MultivariateNormal(mean, covar)


class DKTPlanner(BasePlanner):
    """Wrapper for deep kernel transfer planner in a closed loop
    optimization setting
    """

    def __init__(
        self,
        goal="minimize",
        feas_strategy="naive-0",
        feas_param=0.2,
        batch_size=1,
        random_seed=None,
        num_init_design=5,
        init_design_strategy="random",
        vgp_iters=1000,
        vgp_lr=0.1,
        max_jitter=1e-1,
        cla_threshold=0.5,
        known_constraints=None,
        general_parmeters=None,
        # meta-learning stuff
        warm_start=False,
        model_path="./.tmp_models",
        from_disk=False,
        train_tasks=[],
        valid_tasks=None,
        hyperparams={},
        # moo stuff
        is_moo=False,
        value_space=None,
        scalarizer_kind="Hypervolume",
        moo_params={},
        goals=None,
        **kwargs,
    ):

        local_args = {
            key: val for key, val in locals().items() if key != "self"
        }
        super().__init__(**local_args)

        # meta learning stuff
        self.is_trained = False
        self.warm_start = warm_start
        self.model_path = model_path
        self.from_disk = from_disk
        self._train_tasks = train_tasks
        self._valid_tasks = valid_tasks
        self.hyperparams = hyperparams

        # # NOTE: for maximization, we must flip the signs of the
        # source task values before scaling them
        if self.goal == "maximize":
            self._train_tasks = flip_source_tasks(self._train_tasks)
            self._valid_tasks = flip_source_tasks(self._valid_tasks)

        # if we have a multi-objective problem, scalarize the values
        # for the source tasks individually
        if self.is_moo:
            for task in self._train_tasks:
                scal_values = self.scalarizer.scalarize(task["values"])
                task["values"] = scal_values.reshape(-1, 1)

            for task in self._valid_tasks:
                scal_values = self.scalarizer.scalarize(task["values"])
                task["values"] = scal_values.reshape(-1, 1)

        # instantiate the scaler
        self.scaler = Scaler(
            param_type="normalization",
            value_type="standardization",
        )
        self._train_tasks = self.scaler.fit_transform_tasks(self._train_tasks)
        self._valid_tasks = self.scaler.transform_tasks(self._valid_tasks)

    def _load_model(self):
        # calculate the dimensionality
        x_dim = 0
        for param in self.param_space:
            if param.type in ["continuous", "discrete"]:
                x_dim += 1
            elif param.type == "categorical":
                if param.descriptors[0] is not None:
                    # we have descriptors
                    x_dim += len(param.descriptors[0])
                else:
                    # we dont have descritpors, one hot encodings
                    x_dim += len(param.options)

        self.model = DKT(
            x_dim=x_dim,
            y_dim=1,
            from_disk=self.from_disk,
            model_path=self.model_path,
            hyperparams=self.hyperparams,
        )

    def _meta_train(self):
        """train the model on the source tasks before commencing the
        target optimization
        """
        if not hasattr(self, "model"):
            self._load_model()

        if not self.from_disk:
            # need to meta train
            Logger.log(
                "DKT model has not been meta-trained! Commencing meta-training procedure",
                "WARNING",
            )
            start_time = time.time()
            self.model.train(self._train_tasks, self._valid_tasks)
            training_time = time.time() - start_time
            Logger.log(
                f"Meta-training procedure complete in {training_time:.2f} seconds",
                "INFO",
            )
        else:
            # already meta trained, load from disk
            Logger.log(
                f"Neural process model restored! Skipping meta-training procedure",
                "INFO",
            )

    def _ask(self):
        """query the planner for a batch of new parameter points to measure"""

        # check in the reg model has been meta-trained
        if not hasattr(self, "model"):
            self._meta_train()

        # if we have all nan values, just keep randomly sampling
        if np.logical_or(
            len(self._values) < self.num_init_design,
            np.all(np.isnan(self._values)),
        ):

            # set parameter space for the initial design planner
            self.init_design_planner.set_param_space(self.param_space)

            # sample using initial design strategy (with same batch size)
            return_params = []
            for _ in range(self.batch_size):
                # TODO: this is pretty sloppy - consider standardizing this
                if self.init_design_strategy == "random":
                    self.init_design_planner._tell(
                        iteration=self.num_init_design_completed
                    )
                else:
                    self.init_design_planner.tell()
                rec_params = self.init_design_planner.ask()
                if isinstance(rec_params, list):
                    return_params.append(rec_params[0])
                elif isinstance(rec_params, ParameterVector):
                    return_params.append(rec_params)
                else:
                    raise TypeError
                self.num_init_design_completed += (
                    1  # batch_size always 1 for init design planner
                )
        else:
            # use GP surrogate to propose the samples
            # get the scaled parameters and values for both the regression and classification data
            (
                self.train_x_scaled_cla,
                self.train_y_scaled_cla,
                self.train_x_scaled_reg,
                self.train_y_scaled_reg,
            ) = self.build_train_data()

            use_p_feas_only = False
            # check to see if we are using the naive approaches
            if "naive-" in self.feas_strategy:
                infeas_ix = torch.where(self.train_y_scaled_cla == 1.0)[0]
                feas_ix = torch.where(self.train_y_scaled_cla == 0.0)[0]
                # checking if we have at least one objective function measurement
                #  and at least one infeasible point (i.e. at least one point to replace)
                if np.logical_and(
                    self.train_y_scaled_reg.size(0) >= 1,
                    infeas_ix.shape[0] >= 1,
                ):
                    if self.feas_strategy == "naive-replace":
                        # NOTE: check to see if we have a trained regression surrogate model
                        # if not, wait for the following iteration to make replacements
                        if hasattr(self, "reg_model"):
                            # if we have a trained regression model, go ahead and make replacement
                            new_train_y_scaled_reg = deepcopy(
                                self.train_y_scaled_cla
                            ).double()

                            input = self.train_x_scaled_cla[infeas_ix].double()

                            posterior = self.reg_model.posterior(X=input)
                            pred_mu = posterior.mean.detach()

                            new_train_y_scaled_reg[
                                infeas_ix
                            ] = pred_mu.squeeze(-1)
                            new_train_y_scaled_reg[
                                feas_ix
                            ] = self.train_y_scaled_reg.squeeze(-1)

                            self.train_x_scaled_reg = deepcopy(
                                self.train_x_scaled_cla
                            ).double()
                            self.train_y_scaled_reg = (
                                new_train_y_scaled_reg.view(
                                    self.train_y_scaled_cla.size(0), 1
                                ).double()
                            )

                        else:
                            use_p_feas_only = True

                    elif self.feas_strategy == "naive-0":
                        new_train_y_scaled_reg = deepcopy(
                            self.train_y_scaled_cla
                        ).double()

                        worst_obj = torch.amax(
                            self.train_y_scaled_reg[
                                ~self.train_y_scaled_reg.isnan()
                            ]
                        )

                        to_replace = torch.ones(infeas_ix.size()) * worst_obj

                        new_train_y_scaled_reg[infeas_ix] = to_replace.double()
                        new_train_y_scaled_reg[
                            feas_ix
                        ] = self.train_y_scaled_reg.squeeze()

                        self.train_x_scaled_reg = (
                            self.train_x_scaled_cla.double()
                        )
                        self.train_y_scaled_reg = new_train_y_scaled_reg.view(
                            self.train_y_scaled_cla.size(0), 1
                        )

                    else:
                        raise NotImplementedError
                else:
                    # if we are not able to use the naive strategies, propose randomly
                    # do nothing at all and use the feasibilty surrogate as the acquisition
                    use_p_feas_only = True

            # builds and fits the regression surrogate model
            # self.reg_model = self.build_train_regression_gp(self.train_x_scaled_reg, self.train_y_scaled_reg)

            # builds the regression model
            self.reg_model = DKTModel(
                self.model, self.train_x_scaled_reg, self.train_y_scaled_reg
            )

            if not "naive-" in self.feas_strategy:
                # build and train the classification surrogate model
                (
                    self.cla_model,
                    self.cla_likelihood,
                ) = self.build_train_classification_gp(
                    self.train_x_scaled_cla, self.train_y_scaled_cla
                )

                self.cla_model.eval()
                self.cla_likelihood.eval()

            else:
                self.cla_model, self.cla_likelihood = None, None

            # get the incumbent point
            f_best_argmin = torch.argmin(self.train_y_scaled_reg)
            f_best_scaled = self.train_y_scaled_reg[f_best_argmin][0].float()

            # compute the ratio of infeasible to total points
            infeas_ratio = (
                torch.sum(self.train_y_scaled_cla)
                / self.train_x_scaled_cla.size(0)
            ).item()
            # get the approximate max and min of the acquisition function without the feasibility contribution
            acqf_min_max = self.get_aqcf_min_max(self.reg_model, f_best_scaled)

            if self.batch_size == 1:
                self.acqf = FeasibilityAwareEI(
                    self.reg_model,
                    self.cla_model,
                    self.cla_likelihood,
                    self.param_space,
                    f_best_scaled,
                    self.feas_strategy,
                    self.feas_param,
                    infeas_ratio,
                    acqf_min_max,
                )
            elif self.batch_size > 1:
                self.acqf = FeasibilityAwareQEI(
                    self.reg_model,
                    self.cla_model,
                    self.cla_likelihood,
                    self.param_space,
                    f_best_scaled,
                    self.feas_strategy,
                    self.feas_param,
                    infeas_ratio,
                    acqf_min_max,
                )

            bounds = get_bounds(
                self.param_space,
                self._mins_x,
                self._maxs_x,
                self.has_descriptors,
            )

            print("PROBLEM TYPE : ", self.problem_type)

            # -------------------------------
            # optimize acquisition function
            # -------------------------------

            acquisition_optimizer = AcquisitionOptimizer(
                self.acquisition_optimizer_kind,
                self.param_space,
                self.acqf,
                bounds,
                self.known_constraints,
                self.batch_size,
                self.feas_strategy,
                self.fca_constraint,
                self.has_descriptors,
                self._params,
                self._mins_x,
                self._maxs_x,
            )
            return_params = acquisition_optimizer.optimize()

        return return_params

    def get_aqcf_min_max(self, reg_model, f_best_scaled, num_samples=2000):
        """computes the min and max value of the acquisition function without
        the feasibility contribution. These values will be used to approximately
        normalize the acquisition function
        """
        if self.batch_size == 1:
            acqf = ExpectedImprovement(
                reg_model, f_best_scaled, objective=None, maximize=False
            )
        elif self.batch_size > 1:
            acqf = qExpectedImprovement(
                reg_model, f_best_scaled, objective=None, maximize=False
            )
        samples, _ = propose_randomly(num_samples, self.param_space)
        if (
            not self.problem_type == "fully_categorical"
            and not self.has_descriptors
        ):
            # we dont scale the parameters if we have a one-hot-encoded representation
            samples = forward_normalize(samples, self._mins_x, self._maxs_x)

        acqf_vals = acqf(
            torch.tensor(samples)
            .view(samples.shape[0], 1, samples.shape[-1])
            .double()
        )
        min_ = torch.amin(acqf_vals).item()
        max_ = torch.amax(acqf_vals).item()

        if np.abs(max_ - min_) < 1e-6:
            max_ = 1.0
            min_ = 0.0

        return min_, max_


# DEBUGGING
if __name__ == "__main__":

    from botorch.utils.sampling import draw_sobol_samples
    from botorch.utils.transforms import normalize, unnormalize
    from olympus.campaigns import Campaign, ParameterSpace
    from olympus.objects import (
        ParameterCategorical,
        ParameterContinuous,
        ParameterDiscrete,
    )
    from olympus.surfaces import Surface

    from atlas.utils.synthetic_data import trig_factory

    PARAM_TYPE = "continuous"  #'continuous' # 'mixed', 'categorical'

    PLOT = False

    if PARAM_TYPE == "continuous":

        def surface(x):
            return np.sin(8 * x)

        # define the meta-training tasks
        train_tasks = trig_factory(
            num_samples=20,
            as_numpy=True,
            # scale_range=[[7, 9], [7, 9]],
            # scale_range=[[-9, -7], [-9, -7]],
            scale_range=[[-8.5, -7.5], [7.5, 8.5]],
            shift_range=[-0.02, 0.02],
            amplitude_range=[0.2, 1.2],
        )
        valid_tasks = trig_factory(
            num_samples=5,
            as_numpy=True,
            # scale_range=[[7, 9], [7, 9]],
            # scale_range=[[-9, -7], [-9, -7]],
            scale_range=[[-8.5, -7.5], [7.5, 8.5]],
            shift_range=[-0.02, 0.02],
            amplitude_range=[0.2, 1.2],
        )

        param_space = ParameterSpace()
        # add continuous parameter
        param_0 = ParameterContinuous(name="param_0", low=0.0, high=1.0)
        param_space.add(param_0)

        # variables for optimization
        BUDGET = 50

        planner = DKTPlanner(
            goal="minimize",
            train_tasks=train_tasks,
            valid_tasks=valid_tasks,
            model_path="./tmp_models/",
            from_disk=False,
            hyperparams={
                "model": {
                    "epochs": 2500,
                }
            },
        )

        planner.set_param_space(param_space)

        # make the campaign
        campaign = Campaign()
        campaign.set_param_space(param_space)

        # --------------------------------
        # optional plotting instructions
        # --------------------------------
        if PLOT:
            import matplotlib.pyplot as plt
            import seaborn as sns

            # ground truth, acquisitons
            fig, axes = plt.subplots(1, 3, figsize=(15, 4))
            axes = axes.flatten()
            plt.ion()

            # plotting colors
            COLORS = {
                "ground_truth": "#565676",
                "surrogate": "#A76571",
                "acquisiton": "#c38d94",
                "source_task": "#d8dcff",
            }

        # ---------------------
        # Begin the experiment
        # ---------------------

        for iter in range(BUDGET):

            samples = planner.recommend(campaign.observations)
            print(f"ITER : {iter}\tSAMPLES : {samples}")

            sample_arr = samples.to_array()
            measurement = surface(sample_arr.reshape((1, sample_arr.shape[0])))
            campaign.add_observation(sample_arr, measurement[0])

            # optional plotting
            if PLOT:
                # clear axes
                for ax in axes:
                    ax.clear()

                domain = np.linspace(0, 1, 100)

                # plot all the source tasks
                for task_ix, task in enumerate(train_tasks):
                    if task_ix == len(train_tasks) - 1:
                        label = "source task"
                    else:
                        label = None
                    axes[0].plot(
                        task["params"],
                        task["values"],
                        lw=0.8,
                        alpha=1.0,
                        c=COLORS["source_task"],
                        label=label,
                    )

                # plot the ground truth function
                axes[0].plot(
                    domain,
                    surface(domain),
                    ls="--",
                    lw=3,
                    c=COLORS["ground_truth"],
                    label="ground truth",
                )

                # plot the observations
                params = campaign.observations.get_params()
                values = campaign.observations.get_values()

                # scale the parameters

                if iter > 0:
                    # plot the predicitons of the deep gp
                    context_x = torch.from_numpy(params).float()
                    context_y = torch.from_numpy(values).float()
                    target_x = torch.from_numpy(domain.reshape(-1, 1)).float()

                    mu, sigma, likelihood = planner.model.forward(
                        context_x,
                        context_y,
                        target_x,
                    )
                    lower, upper = likelihood.confidence_region()
                    mu = mu.detach().numpy()

                    # plot the neural process predition
                    axes[1].plot(
                        domain,
                        mu,
                        c="k",
                        lw=4,
                        label="Mnemosyne prediciton",
                    )
                    axes[1].plot(
                        domain,
                        mu,
                        c=COLORS["surrogate"],
                        lw=3,
                        label="DKT prediction",
                    )

                    axes[1].fill_between(
                        domain,
                        lower.detach().numpy(),
                        upper.detach().numpy(),
                        color=COLORS["surrogate"],
                        alpha=0.2,
                    )

                    # plot the acquisition function
                    # evalauate the acquisition function for each proposal
                    acqs = (
                        planner.ei(
                            torch.from_numpy(
                                domain.reshape((domain.shape[0], 1, 1))
                            ).float()
                        )
                        .detach()
                        .numpy()
                    )

                    axes[2].plot(
                        domain,
                        acqs,
                        c="k",
                        lw=4,
                        label="Mnemosyne prediciton",
                    )
                    axes[2].plot(
                        domain,
                        acqs,
                        c=COLORS["acquisiton"],
                        lw=3,
                        label="DKT prediction",
                    )

                for obs_ix, (param, val) in enumerate(zip(params, values)):
                    axes[0].plot(
                        param[0], val[0], marker="o", color="k", markersize=10
                    )
                    axes[0].plot(
                        param[0],
                        val[0],
                        marker="o",
                        color=COLORS["ground_truth"],
                        markersize=7,
                    )

                if len(values) >= 1:
                    # plot the last observation
                    axes[0].plot(
                        params[-1][0],
                        values[-1][0],
                        marker="D",
                        color="k",
                        markersize=11,
                    )
                    axes[0].plot(
                        params[-1][0],
                        values[-1][0],
                        marker="D",
                        color=COLORS["ground_truth"],
                        markersize=8,
                    )
                    # plot horizontal line at last observation location
                    axes[0].axvline(params[-1][0], lw=2, ls=":", alpha=0.8)
                    axes[1].axvline(params[-1][0], lw=2, ls=":", alpha=0.8)

                axes[0].legend()
                axes[1].legend()
                plt.tight_layout()
                # plt.savefig(f'iter_{iter}.png', dpi=300)
                plt.pause(2)

    elif PARAM_TYPE == "categorical":

        # test directly the 2d synthetic Olympus tests

        # -----------------------------
        # Instantiate CatDejong surface
        # -----------------------------
        SURFACE_KIND = "CatCamel"
        surface = Surface(kind=SURFACE_KIND, param_dim=2, num_opts=21)

        tasks = pickle.load(
            open(f"{__datasets__}/catcamel_2D_tasks.pkl", "rb")
        )
        train_tasks = tasks  # tasks[:4] # to make the planner scale

        campaign = Campaign()
        campaign.set_param_space(surface.param_space)

        planner = DKTPlanner(
            goal="minimize",
            train_tasks=train_tasks,
            valid_tasks=train_tasks,
            model_path="./tmp_models/",
            from_disk=False,
            num_init_design=5,
            hyperparams={
                "model": {
                    "epochs": 30000,  # 10000,
                }
            },
            x_dim=42,
        )

        planner.set_param_space(surface.param_space)

        BUDGET = 50

        # ---------------------
        # begin the experiment
        # ---------------------

        for iter in range(BUDGET):

            samples = planner.recommend(campaign.observations)
            print(f"ITER : {iter}\tSAMPLES : {samples}")
            sample = samples[0]
            sample_arr = sample.to_array()
            measurement = np.array(surface.run(sample_arr))
            campaign.add_observation(sample_arr, measurement[0])

    elif PARAM_TYPE == "mixed":

        from summit.benchmarks import MIT_case1
        from summit.strategies import LHS
        from summit.utils.dataset import DataSet

        # perform the simulated rxn example
        BUDGET = 20
        NUM_RUNS = 10
        GOAL = "maximize"

        TARGET_IX = 0  # Case 1
        SOURCE_IX = [1, 2, 3, 4]

        # load the tasks
        tasks = pickle.load(
            open(f"{__datasets__}/dataset_simulated_rxns/tasks_8.pkl", "rb")
        )

        train_tasks = [tasks[i][0] for i in SOURCE_IX]
        valid_tasks = [tasks[i][0] for i in SOURCE_IX]

        # -----------------------
        # build olympus objects
        # -----------------------
        param_space = ParameterSpace()

        # add ligand
        param_space.add(
            ParameterCategorical(
                name="cat_index",
                options=[str(i) for i in range(8)],
                descriptors=[None for i in range(8)],  # add descriptors later
            )
        )
        # add temperature
        param_space.add(
            ParameterContinuous(name="temperature", low=30.0, high=110.0)
        )
        # add residence time
        param_space.add(ParameterContinuous(name="t", low=60.0, high=600.0))
        # add catalyst loading
        # summit expects this to be in nM
        param_space.add(
            ParameterContinuous(
                name="conc_cat",
                low=0.835 / 1000,
                high=4.175 / 1000,
            )
        )

        campaign = Campaign()
        campaign.set_param_space(param_space)

        planner = DKTPlanner(
            goal="minimize",
            train_tasks=train_tasks,
            valid_tasks=valid_tasks,
            model_path="./tmp_models/",
            from_disk=False,
            num_init_design=2,
            hyperparams={
                "model": {
                    "epochs": 15000,
                }
            },
            x_dim=11,
        )

        planner.set_param_space(param_space)

        # begin experiment
        iteration = 0

        while len(campaign.values) < BUDGET:
            print(f"\nITERATION : {iteration}\n")
            # instantiate summit object for evaluation
            exp_pt = MIT_case1(noise_level=1)
            samples = planner.recommend(campaign.observations)
            print(f"SAMPLES : {samples}")
            for sample in samples:
                # turn into dataframe which summit evaluator expects
                columns = ["conc_cat", "t", "cat_index", "temperature"]
                values = {
                    ("conc_cat", "DATA"): sample["conc_cat"],
                    ("t", "DATA"): sample["t"],
                    ("cat_index", "DATA"): sample["cat_index"],
                    ("temperature", "DATA"): sample["temperature"],
                }
                conditions = DataSet([values], columns=columns)

                exp_pt.run_experiments(conditions)

                measurement = exp_pt.data["y"].values[0]
                print("MEASUREMENT : ", measurement)

                # print(exp_pt.data)

                campaign.add_observation(sample, measurement)

            iteration += 1
