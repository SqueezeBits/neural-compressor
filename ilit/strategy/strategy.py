from abc import abstractmethod
import copy
import os
from collections import OrderedDict
from ..adaptor import FRAMEWORKS
from ..objective import OBJECTIVES
from ..metric import METRICS
from ..data import TRANSFORMS
from ..utils.utility import Timeout
from ..utils.create_obj_from_config import create_eval_func
from ..utils import logger
from ..utils.create_obj_from_config import update_config
from pathlib import Path
from datetime import datetime
import pickle

"""The tuning strategies supported by ilit, including basic, random, bayesian and mse.

   User could add new strategies by implementing new TuneStrategy subclass under this directory.
   The naming convention of new strategy subclass should be something like ABCTuneStrategy, user
   could choose this strategy by setting "abc" string in tuning.strategy field of yaml.

   STRATEGIES variable is used to store all implelmented TuneStrategy subclasses to support
   different tuning strategies.
"""
STRATEGIES = {}


def strategy_registry(cls):
    """The class decorator used to register all TuneStrategy subclasses.

    Args:
        cls (class): The class of register.

    Returns:
        cls: The class of register.
    """
    assert cls.__name__.endswith(
        'TuneStrategy'
    ), "The name of subclass of TuneStrategy should end with \'TuneStrategy\' substring."
    if cls.__name__[:-len('TuneStrategy')].lower() in STRATEGIES:
        raise ValueError('Cannot have two strategies with the same name')
    STRATEGIES[cls.__name__[:-len('TuneStrategy')].lower()] = cls
    return cls


class TuneStrategy(object):
    """The base class of tuning strategy.

    Args:
        model (object):                        The FP32 model specified for low precision tuning.
        conf (Conf):                           The Conf class instance initialized from user yaml
                                               config file.
        q_dataloader (generator):              Data loader for calibration, mandatory for
                                               post-training quantization.
                                               It is iterable and should yield a tuple (input,
                                               label) for calibration dataset containing label,
                                               or yield (input, _) for label-free calibration
                                               dataset. The input could be a object, list, tuple or
                                               dict, depending on user implementation, as well as
                                               it can be taken as model input.
        q_func (function, optional):           Reserved for future use.
        eval_dataloader (generator, optional): Data loader for evaluation. It is iterable
                                               and should yield a tuple of (input, label).
                                               The input could be a object, list, tuple or dict,
                                               depending on user implementation, as well as it can
                                               be taken as model input. The label should be able
                                               to take as input of supported metrics. If this
                                               parameter is not None, user needs to specify
                                               pre-defined evaluation metrics through configuration
                                               file and should set "eval_func" paramter as None.
                                               Tuner will combine model, eval_dataloader and
                                               pre-defined metrics to run evaluation process.
        eval_func (function, optional):        The evaluation function provided by user.
                                               This function takes model as parameter, and
                                               evaluation dataset and metrics should be
                                               encapsulated in this function implementation and
                                               outputs a higher-is-better accuracy scalar value.

                                               The pseudo code should be something like:

                                               def eval_func(model):
                                                    input, label = dataloader()
                                                    output = model(input)
                                                    accuracy = metric(output, label)
                                                    return accuracy
        dicts (dict, optional):                The dict containing resume information.
                                               Defaults to None.
    """

    def __init__(self, model, conf, q_dataloader=None, q_func=None,
                 eval_dataloader=None, eval_func=None, dicts=None):
        self.model = model
        self.cfg = conf.usr_cfg
        self.snapshot_path = os.path.abspath(os.path.expanduser(self.cfg.snapshot.path))

        logger.debug('Dump user yaml configuration:')
        logger.debug(self.cfg)

        self.eval_dataloader = eval_dataloader
        self.calib_dataloader = q_dataloader
        self.q_func = q_func
        self.eval_func = eval_func

        framework_specific_info = {'device': self.cfg.device,
                                   'approach': self.cfg.quantization.approach,
                                   'random_seed': self.cfg.tuning.random_seed}
        if self.cfg.framework.name.lower() == 'tensorflow':
            framework_specific_info.update(
                {"inputs": self.cfg.framework.inputs, "outputs": self.cfg.framework.outputs})
        if self.cfg.framework.name.lower() == 'mxnet':
            framework_specific_info.update({"q_dataloader": q_dataloader})

        framework = self.cfg.framework.name.lower()
        self.adaptor = FRAMEWORKS[framework](framework_specific_info)

        self.baseline = None
        self.last_tune_result = None
        self.last_qmodel = None
        self.best_tune_result = None
        self.best_qmodel = None

        objective = self.cfg.tuning.objective.lower()
        self.objective = OBJECTIVES[objective](self.cfg.tuning.accuracy_criterion)

        self.modelwise_tune_space = self._modelwise_tune_space(model, conf)
        self.opwise_tune_space = self._opwise_tune_space(model, conf)
        self.modelwise_tune_cfgs = conf.expand_tune_cfgs(self.modelwise_tune_space)
        self.opwise_tune_cfgs = OrderedDict()
        for key in self.opwise_tune_space:
            self.opwise_tune_cfgs[key] = conf.expand_tune_cfgs(
                self.opwise_tune_space[key])

        self.calib_iter = self.cfg.calibration.iterations

        self.modelwise_quant_cfgs = []
        for cfg in self.modelwise_tune_cfgs:
            if cfg['activation']['dtype'] not in ['fp32', 'bf16']:
                self.modelwise_quant_cfgs.append(cfg)

        self.opwise_quant_cfgs = OrderedDict()
        for key in self.opwise_tune_cfgs:
            cfg_list = self.opwise_tune_cfgs[key]
            new_list = []
            for cfg in cfg_list:
                if cfg['activation']['dtype'] not in ['fp32', 'bf16']:
                    new_list.append(cfg)
            self.opwise_quant_cfgs[key] = new_list

        self.evaluated_cfgs = []

        if dicts is not None:
            self.__dict__.update(dicts)

    @abstractmethod
    def next_tune_cfg(self):
        """The generator of yielding next tuning config to traverse by concrete strategies
           according to last tuning result.

        Yields:
            tune_config (dict): It's a dict containing the tuning configuration to run.
        """
        raise NotImplementedError

    def traverse(self):
        """The main traverse logic, which could be override by some concrete strategy which needs
           more hooks.
        """
        with Timeout(self.cfg.tuning.timeout) as t:
            # get fp32 model baseline
            if self.baseline is None:
                logger.info('Getting FP32 model baseline...')
                self.baseline = self._evaluate(self.model)
            logger.info('FP32 baseline is: ' +
                        ('[{:.4f}, {:.4f}]'.format(*self.baseline) if self.baseline else 'None'))

            for tune_cfg in self.next_tune_cfg():
                evaluated = False
                for cfg in self.evaluated_cfgs:
                    if tune_cfg == cfg[0]:
                        self.last_tune_result = cfg[1]
                        evaluated = True
                if evaluated:
                    logger.debug('Tuning config was evaluated, skip!')
                    continue

                logger.debug('Dump current tuning configuration:')
                logger.debug(tune_cfg)
                self.last_qmodel = self.adaptor.quantize(
                    tune_cfg, self.model, self.calib_dataloader, self.q_func)
                assert self.last_qmodel
                self.last_tune_result = self._evaluate(self.last_qmodel, tune_cfg)

                saved_tune_cfg = copy.deepcopy(tune_cfg)
                saved_last_tune_result = copy.deepcopy(self.last_tune_result)
                self.evaluated_cfgs.append(
                    [saved_tune_cfg, saved_last_tune_result])

                if self.stop(t):
                    break

    def _modelwise_tune_space(self, model, conf):
        """Merge user yaml config with framework model wise capability.

        Args:
            model (object): The FP32 model to tune.
            conf (Conf):    The instance of Conf class.

        Returns:
            dict: The override model wise tunining space
        """
        capability = self.adaptor.query_fw_capability(model)
        dst = capability['modelwise']

        return conf.modelwise_tune_space(dst)

    def _opwise_tune_space(self, model, conf):
        """Generate all tuning spaces for op wise.

        Args:
            model (object): The FP32 model to tune.
            conf (Conf):    The instance of Conf class.

        Returns:
            dict: The opwise tunining space
        """
        capability = self.adaptor.query_fw_capability(model)
        opwise = capability['opwise']

        return conf.opwise_tune_space(opwise)

    def _get_common_cfg(self, model_wise_cfg, op_wise_cfgs):
        """Get the common parts from the model_wise_cfg.
            This function is focused on composing the configuration that consists of
            model-wise field and op-wise unique field data.

        Args:
            model_wise_cfg ([DotDict]): The model-wise configuration.
            op_wise_cfgs ([List]): The list of each op's config in DotDict type.

        Returns:
            [DotDict]: The combined configration with the op-wise unique field.
        """
        model_wise_keys = model_wise_cfg.keys()

        result = op_wise_cfgs[0]
        for each_op_wise_cfg in op_wise_cfgs:
            tmp_cfg = {}
            for k in model_wise_keys:
                tmp_cfg[k] = each_op_wise_cfg[k]

            if model_wise_cfg == tmp_cfg:
                result = each_op_wise_cfg
                break

        return result

    def _evaluate(self, model, tune_cfg=None):
        """The interface of evaluating model.

        Args:
            model (object): The model to be evaluated.

        Returns:
            Objective: The objective value evaluated
        """
        if self.eval_func:
            if self.cfg.tensorboard:

                def eval_func(model):
                    val, _ = self.adaptor.inspect_tensor(
                        model,
                        eval_func=self.eval_func,
                        to_tensorboard=True,
                        tune_cfg=tune_cfg)
                    return val

                val = self.objective.evaluate(eval_func, model)
            else:
                val = self.objective.evaluate(self.eval_func, model)
        else:
            assert self.cfg.tuning.metric is not None, \
                'metric field of tuning should not be empty'

            postprocess_cfg = self.cfg.postprocess if self.cfg.evaluation is None \
                else update_config(self.cfg.evaluation.postprocess, \
                                   self.cfg.postprocess)

            eval_func = create_eval_func(self.cfg.framework.name, \
                                         self.eval_dataloader, \
                                         self.adaptor, \
                                         self.cfg.tuning.metric, \
                                         postprocess_cfg)

            val = self.objective.evaluate(eval_func, model)
        return val

    def __getstate__(self):
        """Magic method for pickle saving.

        Returns:
            dict: Saved dict for resuming
        """
        save_dict = {
            'baseline': self.baseline,
            'cfg': self.cfg,
            'last_tune_result': self.last_tune_result,
            'best_tune_result': self.best_tune_result,
            'modelwise_tune_space': self.modelwise_tune_space,
            'opwise_tune_space': self.opwise_tune_space,
            'modelwise_tune_cfgs': self.modelwise_tune_cfgs,
            'opwise_tune_cfgs': self.opwise_tune_cfgs,
            'modelwise_quant_cfgs': self.modelwise_quant_cfgs,
            'opwise_quant_cfgs': self.opwise_quant_cfgs,
            'evaluated_cfgs': self.evaluated_cfgs
        }
        return save_dict

    def __setstate__(self, d):
        """Magic method for pickle loading.

        Args:
            d (dict): The dict to load.
        """
        self.__dict__.update(d)

    def _save(self, file_name=None):
        """save current tuning state to snapshot for resuming.
        """
        path = Path(self.snapshot_path)
        path.mkdir(exist_ok=True, parents=True)

        if file_name is not None:
            fname = self.snapshot_path + '/ilit-' + file_name + '.snapshot'
        else:
            fname = self.snapshot_path + '/ilit-' + datetime.today().strftime(
                '%Y-%m-%d-%H-%M-%S') + '.snapshot'
        with open(fname, 'wb') as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("Save snapshot to {}".format(os.path.abspath(fname)))

    def stop(self, timeout):
        """Check if need to stop traversing the tuning space, either accuracy goal is met
           or timeout is reach.

        Args:
            timeout (Timeout): The timeout object instantiated in utils.py

        Returns:
            bool: True if need stop, otherwise False
        """
        need_stop = False

        if self.objective.compare(self.best_tune_result, self.baseline):
            del self.best_tune_result
            del self.best_qmodel
            self.best_tune_result = self.last_tune_result
            self.best_qmodel = self.last_qmodel
        else:
            del self.last_qmodel

        logger.info(
            'Tune result is: ' +
            ('[{:.4f}, {:.4f}]'.format(
                *self.last_tune_result) if self.last_tune_result else 'None') +
            ' Best tune result is: ' +
            ('[{:.4f}, {:.4f}]'.format(
                *self.best_tune_result) if self.best_tune_result else 'None'))

        if timeout.seconds != 0 and timeout.timed_out:
            need_stop = True
        elif timeout.seconds == 0 and self.best_tune_result:
            need_stop = True
        else:
            need_stop = False

        return need_stop
