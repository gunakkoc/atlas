#!/usr/bin/env python

import numpy as np
import pytest
from olympus.campaigns import Campaign, ParameterSpace
from olympus.datasets import Dataset
from olympus.emulators import Emulator
from olympus.objects import (
    ParameterCategorical,
    ParameterContinuous,
    ParameterDiscrete,
    ParameterVector,
)
from olympus.scalarizers import Scalarizer
from olympus.surfaces import Surface

from atlas.optimizers.gp.planner import BoTorchPlanner
from atlas.optimizers.qnehvi.planner import qNEHVIPlanner

CONT = {
    "init_design_strategy": [
        "random",
    ],  # init design strategues
    "batch_size": [1, 2],  # batch size
    "feas_strategy_param": [
        "naive-0_0",
        "fia_1000",
        "fwa_0",
        "fca_0.2",
        "fca_0.8",
        "fia_0.5",
        "fia_2.0",
    ],
    "use_descriptors": [False],  # use descriptors
}

CAT = {
    "init_design_strategy": [
        "random",
    ],  # init design strategues
    "batch_size": [1, 2],  # batch size
    "feas_strategy_param": [
        "naive-0_0",
        "fia_1000",
        "fwa_0",
        "fca_0.2",
        "fca_0.8",
        "fia_0.5",
        "fia_2.0",
    ],
    "use_descriptors": [False, True],  # use descriptors
}


@pytest.mark.parametrize("init_design_strategy", CONT["init_design_strategy"])
@pytest.mark.parametrize("batch_size", CONT["batch_size"])
@pytest.mark.parametrize("feas_strategy_param", CONT["feas_strategy_param"])
@pytest.mark.parametrize("use_descriptors", CONT["use_descriptors"])
def test_unknown_cont(
    init_design_strategy, batch_size, feas_strategy_param, use_descriptors
):
    run_continuous(
        init_design_strategy, batch_size, feas_strategy_param, use_descriptors
    )


@pytest.mark.parametrize("init_design_strategy", CAT["init_design_strategy"])
@pytest.mark.parametrize("batch_size", CAT["batch_size"])
@pytest.mark.parametrize("feas_strategy_param", CAT["feas_strategy_param"])
@pytest.mark.parametrize("use_descriptors", CAT["use_descriptors"])
def test_unknown_cat(
    init_design_strategy, batch_size, feas_strategy_param, use_descriptors
):
    run_categorical(
        init_design_strategy, batch_size, feas_strategy_param, use_descriptors
    )


def run_continuous(
    init_design_strategy,
    batch_size,
    feas_strategy_param,
    use_descriptors,
    num_init_design=5,
):
    def surface(x):
        if np.random.uniform() > 0.2:
            return (
                np.sin(8 * x[0]) - 2 * np.cos(6 * x[1]) + np.exp(-2.0 * x[2])
            )
        else:
            return np.nan

    split = feas_strategy_param.split("_")
    feas_strategy, feas_param = split[0], float(split[1])

    param_space = ParameterSpace()
    param_0 = ParameterContinuous(name="param_0", low=0.0, high=1.0)
    param_1 = ParameterContinuous(name="param_1", low=0.0, high=1.0)
    param_2 = ParameterContinuous(name="param_2", low=0.0, high=1.0)
    param_space.add(param_0)
    param_space.add(param_1)
    param_space.add(param_2)

    planner = BoTorchPlanner(
        goal="minimize",
        feas_strategy=feas_strategy,
        feas_param=feas_param,
        init_design_strategy=init_design_strategy,
        num_init_design=num_init_design,
        batch_size=batch_size,
    )

    planner.set_param_space(param_space)

    campaign = Campaign()
    campaign.set_param_space(param_space)

    BUDGET = num_init_design + batch_size * 4

    while len(campaign.observations.get_values()) < BUDGET:

        samples = planner.recommend(campaign.observations)
        for sample in samples:
            sample_arr = sample.to_array()
            measurement = surface(sample_arr)
            campaign.add_observation(sample_arr, measurement)

    assert len(campaign.observations.get_params()) == BUDGET
    assert len(campaign.observations.get_values()) == BUDGET


def run_categorical(
    init_design_strategy,
    batch_size,
    feas_strategy_param,
    use_descriptors,
    num_init_design=5,
):

    surface_kind = "CatDejong"
    surface = Surface(kind=surface_kind, param_dim=2, num_opts=21)

    split = feas_strategy_param.split("_")
    feas_strategy, feas_param = split[0], float(split[1])

    campaign = Campaign()
    campaign.set_param_space(surface.param_space)

    planner = BoTorchPlanner(
        goal="minimize",
        feas_strategy=feas_strategy,
        feas_param=feas_param,
        init_design_strategy=init_design_strategy,
        num_init_design=num_init_design,
        batch_size=batch_size,
        use_descriptors=use_descriptors,
    )
    planner.set_param_space(surface.param_space)

    BUDGET = num_init_design + batch_size * 4

    while len(campaign.observations.get_values()) < BUDGET:

        samples = planner.recommend(campaign.observations)
        for sample in samples:
            sample_arr = sample.to_array()
            if np.random.uniform() > 0.2:
                measurement = surface.run(sample_arr)[0][0]
            else:
                measurement = np.nan
            # print(sample, measurement)
            campaign.add_observation(sample_arr, measurement)

    assert len(campaign.observations.get_params()) == BUDGET
    assert len(campaign.observations.get_values()) == BUDGET



def run_qnehvi_mixed_cat_disc(
    init_design_strategy,
    batch_size,
    feas_strategy_param,
    use_descriptors,
    num_init_design=10,
):

    def surface(x):
        if x["param_0"] == "x0":
            factor = 0.1
        elif x["param_0"] == "x1":
            factor = 1.0
        elif x["param_0"] == "x2":
            factor = 10.0

        return np.array([
            # objective 1
            np.sin(8.0 * x["param_1"])
            - 2.0 * np.cos(6.0 * x["param_1"])
            + np.exp(-2.0 * x["param_2"])
            + 2.0 * (1.0 / factor), 
            # objective 2
             np.sin(8.0 * x["param_1"])
            + 2.0 * np.cos(6.0 * x["param_1"])
            - np.exp(-5.0 * x["param_2"])
            + 2.0 * (2.0 / factor), 
        ])
    
    split = feas_strategy_param.split("_")
    feas_strategy, feas_param = split[0], float(split[1])

    if use_descriptors:
        desc_param_0 = [[float(i), float(i)] for i in range(3)]
    else:
        desc_param_0 = [None for i in range(3)]

    param_space = ParameterSpace()
    param_0 = ParameterCategorical(
        name="param_0",
        options=["x0", "x1", "x2"],
        descriptors=desc_param_0,
    )
    param_1 = ParameterDiscrete(
        name="param_1",
        options=[0.0, 0.25, 0.5, 0.75, 1.0],
    )
    param_2 = ParameterDiscrete(
        name="param_2",
        options=[0.0, 0.25, 0.5, 0.75, 1.0],
    )
    param_space.add(param_0)
    param_space.add(param_1)
    param_space.add(param_2)

    value_space = ParameterSpace()
    value_space.add(ParameterContinuous(name='obj0'))
    value_space.add(ParameterContinuous(name='obj1'))


    planner = qNEHVIPlanner(
        goal='minimize', 
        feas_strategy=feas_strategy,
        feas_param=feas_param,
        batch_size=batch_size,
        use_descriptors=use_descriptors,
        init_design_strategy=init_design_strategy,
        num_init_design=num_init_design,
        acquisition_optimizer_kind='gradient',
        is_moo=True,
        value_space=value_space,
        goals = ['min', 'max']
    )


    planner.set_param_space(param_space)

    campaign = Campaign()
    campaign.set_param_space(param_space)
    campaign.set_value_space(value_space)

    BUDGET = num_init_design + batch_size * 4 

    while len(campaign.observations.get_values()) < BUDGET:

        samples = planner.recommend(campaign.observations)
        for sample in samples:
            if np.random.uniform() > 0.35:
                measurement = surface(sample)
            else:
                measurement = np.array([np.nan, np.nan])
            campaign.add_observation(sample, measurement)
    

    assert len(campaign.observations.get_params()) == BUDGET
    assert len(campaign.observations.get_values()) == BUDGET




if __name__ == "__main__":

    # run_continuous('random', 1, 'fwa-0', False)
    # run_continuous('random', 1, 'naive-0_0', False)
    
    #run_qnehvi_mixed_cat_disc('random', 1, 'naive-0_0', False)
    #run_qnehvi_mixed_cat_disc('random', 1, 'fia_1', False)
    run_qnehvi_mixed_cat_disc('random', 2, 'fia_1', False)
    #run_qnehvi_mixed_cat_disc('random', 1, 'fca_0.5', False)