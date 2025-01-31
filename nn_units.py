# -*- coding: utf-8 -*-
"""
.. invisible:
     _   _ _____ _     _____ _____
    | | | |  ___| |   |  ___/  ___|
    | | | | |__ | |   | |__ \ `--.
    | | | |  __|| |   |  __| `--. \
    \ \_/ / |___| |___| |___/\__/ /
     \___/\____/\_____|____/\____/

Created on Jan 28, 2014

Base Forward and Backward Units for Neural Networks

███████████████████████████████████████████████████████████████████████████████

Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.

███████████████████████████████████████████████████████████████████████████████
"""


from __future__ import division
from collections import defaultdict
import gc
import numpy
import logging
import time
import six
from zope.interface import implementer

from veles.avatar import Avatar
from veles.external.prettytable import PrettyTable
from veles.distributable import IDistributable
from veles.loader import Loader
from veles.memory import reshape_transposed, roundup, Array
from veles.mutable import Bool
from veles.accelerated_units import AcceleratedUnit, AcceleratedWorkflow
import veles.prng as prng
from veles.units import UnitCommandLineArgumentsRegistry
from veles.workflow import Repeater
from veles.snapshotter import SnapshotterBase, SnapshotterToFile, \
    SnapshotterToDB
from veles.timeit2 import timeit
from veles.znicz.decision import DecisionBase
from veles.znicz.evaluator import EvaluatorBase


class Match(list):
    @property
    def forward(self):
        for item in self:
            if issubclass(item, ForwardBase):
                return item
        raise IndexError()

    @property
    def has_forward(self):
        for item in self:
            if issubclass(item, ForwardBase):
                return True
        return False

    @property
    def backwards(self):
        for item in self:
            if not issubclass(item, ForwardBase):
                yield item


class MatchingObject(UnitCommandLineArgumentsRegistry):
    mapping = defaultdict(Match)
    logger = logging.getLogger("Matcher")

    def __init__(cls, name, bases, clsdict):
        super(MatchingObject, cls).__init__(name, bases, clsdict)
        if not MatchingObject.enabled:
            return
        mapping = clsdict.get('MAPPING', None)
        if mapping is None:
            MatchingObject.logger.warning("%s does not have MAPPING", cls)
            return
        if not isinstance(mapping, set):
            raise TypeError("%s: MAPPING must be of type 'set'" % cls)
        for val in mapping:
            match = MatchingObject.mapping[val]
            if issubclass(cls, Forward) and match.has_forward and \
                    cls != match.forward:
                raise ValueError(
                    "%s: attempted to add a second Forward %s to %s" %
                    (val, cls, match.forward))
            match.append(cls)


@six.add_metaclass(MatchingObject)
class ForwardBase(AcceleratedUnit):
    """Base class for forward propagation units.
    """
    hide_from_registry = True
    MAPPING = set()


@implementer(IDistributable)
class Forward(ForwardBase):
    """Class for forward propagation units.

    Attributes:
        input: input layer values.
        output: output layer values.
        weights: weights.
        bias: bias.
        weights_stddev: magnitude of the random distribution for weights.
        bias_stddev: magnitude of the random distribution for bias.
        rand: prng.Rand() object for initial weights generation.
    """
    hide_from_registry = True
    MAPPING = set()

    def __init__(self, workflow, **kwargs):
        kwargs["view_group"] = kwargs.get("view_group", "WORKER")
        super(Forward, self).__init__(workflow, **kwargs)
        self.weights_stddev = kwargs.get("weights_stddev")
        self.bias_stddev = kwargs.get("bias_stddev", self.weights_stddev)
        self.weights_filling = kwargs.get("weights_filling", "uniform")
        self.bias_filling = kwargs.get("bias_filling", "uniform")
        self.rand = kwargs.get("rand", prng.get())
        self.weights_transposed = kwargs.get("weights_transposed", False)
        self.include_bias = kwargs.get("include_bias", True)
        self.demand("input")
        self.output = Array(shallow_pickle=True)
        self.weights = Array()
        self.bias = Array()
        self.forward_mode = False
        self.exports = ["weights", "bias", "include_bias",
                        "weights_transposed"]

    def package_export(self):
        data = {}
        for attr in self.exports:
            value = getattr(self, attr)
            if value is not None:
                if isinstance(value, Array):
                    value.map_read()
                    value = value.mem
                data[attr] = value
        return data

    @property
    def forward_mode(self):
        return self._forward_mode

    @forward_mode.setter
    def forward_mode(self, value):
        if not isinstance(value, bool):
            raise TypeError(
                "forward_mode must be boolean (got %s)" % type(value))
        self._forward_mode = value

    def initialize(self, device, **kwargs):
        self.forward_mode = kwargs.get("forward_mode", False)
        super(Forward, self).initialize(device=device, **kwargs)

    def generate_data_for_slave(self, slave):
        if self.forward_mode:
            return None
        data = [None, None]
        if self.weights:
            self.weights.map_read()
            data[0] = self.weights.mem
        if self.bias:
            self.bias.map_read()
            data[1] = self.bias.mem
        return data

    def generate_data_for_master(self):
        return None

    def apply_data_from_master(self, data):
        if self.forward_mode:
            return
        if self.weights:
            self.weights.map_invalidate()
            numpy.copyto(self.weights.mem, data[0])
        else:
            self.weights.reset(data[0])
        if self.bias:
            self.bias.map_invalidate()
            numpy.copyto(self.bias.mem, data[1])
        else:
            self.bias.reset(data[1])

    def apply_data_from_slave(self, data, slave):
        pass

    def drop_slave(self, slave):
        pass


class NNLayerBase(Forward):
    MAPPING = set()

    def print_debug_data(self, t_start):
        """Show some statistics.
        """
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        self.output.map_read()
        y = self.output.mem
        if y.dtype in (numpy.complex64, numpy.complex128):
            self.debug(
                "%s: %d samples with %d weights in %.2f sec: "
                "y: min avg max: %.6f %.6f %.6f" %
                (self.__class__.__name__, y.shape[0],
                 self.weights.mem.size, time.time() - t_start,
                 min(y.real.min(), y.imag.min()),
                 (numpy.average(y.real) + numpy.average(y.imag)) * 0.5,
                 max(y.real.max(), y.imag.max())))
        else:
            self.debug(
                "%s: %d samples with %d weights in %.2f sec: "
                "y: min avg max: %.6f %.6f %.6f" %
                (self.__class__.__name__, y.shape[0],
                 self.weights.mem.size, time.time() - t_start,
                 y.min(), numpy.average(y), y.max()))

    def ocl_run(self):
        """Forward propagation from batch on GPU.
        """
        self.unmap_vectors(self.output, self.input, self.weights, self.bias)
        self.execute_kernel(self._global_size, self._local_size)


class FullyConnectedOutput(object):
    """Contains properties for fully connected layer's output.
    """
    def __init__(self, *args, **kwargs):
        super(FullyConnectedOutput, self).__init__(*args, **kwargs)
        self.output_sample_shape = kwargs.get("output_sample_shape", tuple())
        self.output_samples_number = kwargs.get("output_samples_number")
        self.output_dtype = kwargs.get("output_dtype")

    @property
    def output_sample_shape(self):
        return self._output_sample_shape

    @output_sample_shape.setter
    def output_sample_shape(self, value):
        assert not self.is_initialized, \
            "Cannot set output_sample_shape after initialize() was called"
        self._set_output_sample_shape(value)

    def _set_output_sample_shape(self, value):
        if isinstance(value, int):
            self._output_sample_shape = (value,)
        elif hasattr(value, "shape"):
            self._output_sample_shape = value.shape[1:]
        elif hasattr(value, "__iter__"):
            self._output_sample_shape = tuple(value)
        else:
            raise TypeError("Unsupported output_sample_shape type: %s" %
                            type(value))

    @property
    def output_samples_number(self):
        if self.input:
            return self.input.shape[0]
        return self._output_samples_number

    @output_samples_number.setter
    def output_samples_number(self, value):
        if value is not None and not isinstance(value, int):
            raise TypeError("output_samples_number must be an integer")
        self._output_samples_number = value

    @property
    def output_shape(self):
        return (self.output_samples_number,) + self.output_sample_shape

    @property
    def neurons_number(self):
        return int(numpy.prod(self.output_sample_shape))


class GradientDescentWithActivation(AcceleratedUnit):
    hide_from_registry = True

    def __init__(self, workflow, **kwargs):
        super(GradientDescentWithActivation, self).__init__(workflow, **kwargs)
        self.krn_err_output_name = None
        self.demand("output")

    def initialize(self, device, **kwargs):
        assert (isinstance(self.krn_err_output_name, str) and
                self.krn_err_output_name)
        assert self.err_output.shape == self.output.shape
        retval = super(GradientDescentWithActivation, self).initialize(
            device, **kwargs)
        if retval:
            return retval
        self.output.initialize(device)
        return retval

    def ocl_init(self):
        super(GradientDescentWithActivation, self).ocl_init()
        self.krn_err_output_ = self.get_kernel(self.krn_err_output_name)
        self.krn_err_output_.set_args(self.err_output.devmem,
                                      self.output.devmem)
        self._global_size_err_output = (self.err_output.size,)
        self._local_size_err_output = None

    def cuda_init(self):
        super(GradientDescentWithActivation, self).cuda_init()
        self.krn_err_output_ = self.get_kernel(self.krn_err_output_name)
        self.krn_err_output_.set_args(self.err_output.devmem,
                                      self.output.devmem)
        block_size = self.device.suggest_block_size(self.krn_err_output_)
        self._global_size_err_output = (int(numpy.ceil(
            self.err_output.size / block_size)), 1, 1)
        self._local_size_err_output = (block_size, 1, 1)


@implementer(IDistributable)
@six.add_metaclass(MatchingObject)
class GradientDescentBase(AcceleratedUnit):
    """Base class for gradient descent units.

    Attributes:
        input: input layer values.
        output: output layer values.
        err_output: error to backpropagate.
        err_input: backpropagated error.
        weights: weights.
        bias: bias.
        batch_size: current minibatch size.
        learning_rate: gradient descent speed (positive).
        learning_rate_bias
        weights_decay: regularization for weights (see l1_vs_l2).
        weights_decay_bias
        gradient_moment: moment coefficient for weights.
        gradient_moment_bias
        gradient_weights_with_moment: accumulated moment.
        gradient_bias_with_moment
        batch_size: effective batch size (if None, get it from y).
        weights_transposed: assume weights matrix as a transposed one.
        apply_gradient: will apply gradient.
        gradient_changed: when True, slave will send gradients to master
            (assigned to True just before the run call, so it can be set to
            False inside ocl_run, numpy_run if necessary).
        ocl_set_const_args: True when constant arguments for the kernel
                            had been changed and need to be set again.
    """
    hide_from_registry = True
    MAPPING = set()

    REDUCE_SIZE = 64  # used for updating bias

    def __init__(self, workflow, **kwargs):
        kwargs["view_group"] = kwargs.get("view_group", "TRAINER")
        super(GradientDescentBase, self).__init__(workflow, **kwargs)
        self.err_input = Array(shallow_pickle=True)
        self.ocl_set_const_args = True
        self.weights = None
        self.bias = None
        self.output = None
        self.demand("input", "err_output")
        self.learning_rate = kwargs.get("learning_rate", 0.01)
        self.learning_rate_bias = kwargs.get("learning_rate_bias",
                                             self.learning_rate)
        self.weights_decay = kwargs.get("weights_decay", 0.00005)
        self.weights_decay_bias = kwargs.get("weights_decay_bias", 0.0)
        self.l1_vs_l2 = kwargs.get("l1_vs_l2", 0)
        self.l1_vs_l2_bias = kwargs.get("l1_vs_l2_bias", self.l1_vs_l2)
        self.gradient_moment = kwargs.get("gradient_moment", 0)
        self.gradient_moment_bias = kwargs.get("gradient_moment_bias",
                                               self.gradient_moment)
        self.weights_transposed = kwargs.get("weights_transposed", False)

        # err_input = alpha * new_err_input + beta * err_input
        self.err_input_alpha = kwargs.get("err_input_alpha", 1.0)
        self.err_input_beta = kwargs.get("err_input_beta", 0.0)

        # Calculate err_input or not
        # (when False during initialize, memory will not be allocated)
        self.need_err_input = kwargs.get("need_err_input", True)
        # Calculate gradient for weights and bias or not
        # (when False during initialize, memory will not be allocated)
        self.need_gradient_weights = kwargs.get("need_gradient_weights", True)

        # Use bias or not
        self.include_bias = kwargs.get("include_bias", True)

        # Experimental regularization
        self.factor_ortho = kwargs.get("factor_ortho", 0)
        self.col_sums = Array()  # for orthogonalization

        # Current gradient as it is without applying learning_rate etc.
        self.gradient_weights = Array()
        self.gradient_bias = Array()

        # Gradient with applied learning_rate etc.
        # optionally accumulated from the previous run
        self.accumulate_gradient = kwargs.get("accumulate_gradient", False)

        # When accumulate_gradient set to True:
        # 1. Calculate gd
        # 2. acc = acc_alpha * gd + acc_beta * acc
        # 3. gd = gd_alpha * acc + gd_beta * gd
        # 4. Apply moments to gd
        # 5. weights += gd if apply_gradient set to True
        self.acc_alpha = kwargs.get("acc_alpha", 0.0)
        self.acc_beta = kwargs.get("acc_beta", 0.0)
        self.gd_alpha = kwargs.get("gd_alpha", 0.0)
        self.gd_beta = kwargs.get("gd_beta", 1.0)

        self.accumulated_gradient_weights = Array()
        self.accumulated_gradient_bias = Array()

        # Gradient with accumulated moments
        self.gradient_weights_with_moment = Array()
        self.gradient_bias_with_moment = Array()

        # Sets to True when gradient changes
        self.gradient_changed = False

        # Gradient will be applied to weights immediately just after computing
        self.apply_gradient = kwargs.get("apply_gradient",
                                         not workflow.is_slave)

    @property
    def current_batch_size(self):
        batch_size = getattr(self, "batch_size", None)
        if batch_size is None:
            return self.err_output.mem.shape[0]
        return int(batch_size)

    def initialize(self, device, **kwargs):
        super(GradientDescentBase, self).initialize(device, **kwargs)

        if self.weights:
            assert len(self.weights.shape) == 2
            self.weights_shape = (tuple(reversed(self.weights.shape))
                                  if self.weights_transposed
                                  else self.weights.shape)
        else:
            self.weights_shape = None

        self.learning_rate = kwargs.get("learning_rate", self.learning_rate)
        self.weights_decay = kwargs.get("weights_decay", self.weights_decay)
        self.gradient_moment = kwargs.get("gradient_moment",
                                          self.gradient_moment)
        self.learning_rate_bias = kwargs.get("learning_rate_bias",
                                             self.learning_rate_bias)
        self.weights_decay_bias = kwargs.get("weights_decay_bias",
                                             self.weights_decay_bias)
        self.gradient_moment_bias = kwargs.get("gradient_moment_bias",
                                               self.gradient_moment_bias)

        if self.need_gradient_weights and self.weights:
            if not self.gradient_weights:
                self.gradient_weights.reset(numpy.zeros_like(self.weights.mem))
            else:
                assert self.gradient_weights.size == self.weights.size

        if (self.need_gradient_weights and self.weights and
                self.accumulate_gradient):
            if not self.accumulated_gradient_weights:
                self.accumulated_gradient_weights.reset(
                    numpy.zeros_like(self.weights.mem))
            else:
                assert (self.accumulated_gradient_weights.size ==
                        self.weights.size)

        if (self.need_gradient_weights and self.weights and
                (self.gradient_moment or not self.is_standalone)):
            if not self.gradient_weights_with_moment:
                self.gradient_weights_with_moment.reset(
                    numpy.zeros_like(self.weights.mem))
            else:
                assert self.gradient_weights_with_moment.size == \
                    self.weights.size

        if (self.need_gradient_weights and self.include_bias and self.bias and
            (not self.gradient_bias or
             self.gradient_bias.size != self.bias.size)):
            self.gradient_bias.reset(numpy.zeros_like(self.bias.mem))

        if (self.need_gradient_weights and self.include_bias and self.bias and
            self.accumulate_gradient and
            (not self.accumulated_gradient_bias or
             self.accumulated_gradient_bias.size != self.bias.size)):
            self.accumulated_gradient_bias.reset(numpy.zeros_like(
                self.bias.mem))

        if (self.need_gradient_weights and self.include_bias and self.bias and
                (self.gradient_moment_bias or not self.is_standalone)):
            if not self.gradient_bias_with_moment:
                self.gradient_bias_with_moment.reset(
                    numpy.zeros_like(self.bias.mem))
            else:
                assert self.gradient_bias_with_moment.size == self.bias.size

        dtype = self.err_output.dtype
        if self.need_err_input:
            if self.err_input:
                assert self.err_input.shape[1:] == self.input.shape[1:]
            if (not self.err_input or
                    self.err_input.shape[0] != self.input.shape[0]):
                self.err_input.reset(numpy.zeros(self.input.shape, dtype))

        if self.need_gradient_weights and self.weights:
            side = self.weights_shape[0]
            other = self.weights.size // side
            if self.factor_ortho:
                if not self.col_sums:
                    self.col_sums.reset(numpy.zeros(other, dtype=dtype))
                else:
                    assert self.col_sums.size == other
                self.col_sums.initialize(self.device)
            self.reduce_size = roundup(min(self.reduce_size, other), 32)
            self.weights.initialize(self.device)

        self.init_vectors(
            self.err_output, self.weights, self.bias, self.input, self.output,
            self.err_input, self.gradient_weights, self.gradient_bias,
            self.accumulated_gradient_weights, self.accumulated_gradient_bias,
            self.gradient_weights_with_moment, self.gradient_bias_with_moment)

    def gpu_weights_update(self):
        if not self.need_gradient_weights:
            return

        self.unmap_vectors(
            self.input, self.err_output, self.weights,
            self.gradient_weights, self.accumulated_gradient_weights,
            self.gradient_weights_with_moment)

        if self.factor_ortho:
            self.col_sums.unmap()
            self.execute_kernel(
                self._global_size_ortho, self._local_size_ortho,
                self.krn_compute_col_sums_)

            self._weights_const[12] = self.factor_ortho
            self.krn_weights_.set_arg(12, self._weights_const[12:13])

        self._weights_const[4:12] = (
            self.learning_rate, self.weights_decay, self.l1_vs_l2,
            self.gradient_moment, self.acc_alpha, self.acc_beta,
            self.gd_alpha, self.gd_beta)
        self.krn_weights_.set_args(
            self.device.skip(4), self._weights_const[4:5],
            self._weights_const[5:6], self._weights_const[6:7],
            self._weights_const[7:8], self._weights_const[8:9],
            self._weights_const[9:10], self._weights_const[10:11],
            self._weights_const[11:12])

        self.execute_kernel(
            self._global_size_weights, self._local_size_weights,
            self.krn_weights_)

    def gpu_bias_update(self):
        if not self.need_gradient_weights or not self.include_bias:
            return

        self.unmap_vectors(
            self.err_output, self.bias, self.gradient_bias,
            self.accumulated_gradient_bias, self.gradient_bias_with_moment)

        self._bias_const[5:13] = (
            self.learning_rate_bias, self.weights_decay_bias,
            self.l1_vs_l2_bias, self.gradient_moment_bias,
            self.acc_alpha, self.acc_beta,
            self.gd_alpha, self.gd_beta)
        self.krn_bias_.set_args(
            self.device.skip(5), self._bias_const[5:6], self._bias_const[6:7],
            self._bias_const[7:8], self._bias_const[8:9],
            self._bias_const[9:10], self._bias_const[10:11],
            self._bias_const[11:12], self._bias_const[12:13])

        self.execute_kernel(
            self._global_size_bias, self._local_size_bias,
            self.krn_bias_)

    def gpu_err_output_update(self):
        """Multiply err_output by activation derivative by output.
        """
        if self.krn_err_output_ is None:
            return
        self.err_output.unmap()
        self.output.unmap()
        self.execute_kernel(
            self._global_size_err_output, self._local_size_err_output,
            self.krn_err_output_)

    def numpy_err_output_update(self):
        """Multiply err_output by activation derivative by output.
        """
        pass

    def print_debug_data(self):
        """
        Show weights statistics
        """
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        self.weights.map_read()
        self.bias.map_read()
        self.gradient_bias.map_read()
        self.gradient_weights.map_read()
        weights = self.weights.mem
        bias = self.bias.mem
        grad_weights = self.gradient_weights.mem
        grad_bias = self.gradient_bias.mem

        weight_table = PrettyTable("TYPE", "Mean", "StdDev", "Min", "Max")
        weight_table.float_format = ".10"
        for (w_name, w_array) in [("Weight", weights), ("Bias", bias),
                                  ("Grad Weight", grad_weights),
                                  ("Grad Bias", grad_bias)]:
            w_mean = w_stddev = w_min = w_max = None
            if w_array is not None and w_array.size > 0:
                w_mean = numpy.mean(w_array)
                w_stddev = numpy.std(w_array)
                w_min = numpy.min(w_array)
                w_max = numpy.max(w_array)
            weight_table.add_row(w_name, w_mean, w_stddev, w_min, w_max)
        self.debug("\n" + weight_table.get_string())

    def generate_data_for_slave(self, slave):
        return (self.learning_rate, self.weights_decay, self.gradient_moment,
                self.learning_rate_bias, self.weights_decay_bias,
                self.gradient_moment_bias)

    @staticmethod
    def fill_zeros(vector):
        if not vector:
            return
        vector.map_invalidate()
        vector.mem[:] = 0

    def apply_data_from_master(self, data):
        self.learning_rate = data[0]
        self.weights_decay = data[1]
        self.gradient_moment = data[2]
        self.learning_rate_bias = data[3]
        self.weights_decay_bias = data[4]
        self.gradient_moment_bias = data[5]
        self.fill_zeros(self.gradient_weights_with_moment)
        self.fill_zeros(self.gradient_bias_with_moment)
        self.fill_zeros(self.gradient_weights)
        self.fill_zeros(self.gradient_bias)
        self.fill_zeros(self.accumulated_gradient_weights)
        self.fill_zeros(self.accumulated_gradient_bias)

    def generate_data_for_master(self):
        if not self.gradient_changed:
            return None
        self.gradient_changed = False
        self.gradient_weights_with_moment.map_read()
        self.gradient_bias_with_moment.map_read()
        return (self.gradient_weights_with_moment.mem,
                self.gradient_bias_with_moment.mem)

    def apply_data_from_slave(self, data, slave):
        if self.weights:
            self.weights.map_write()
            self.gradient_weights_with_moment.map_write()
            self.gradient_weights_with_moment.mem *= self.gradient_moment
            self.gradient_weights_with_moment.mem += data[0]
            self.weights.mem += self.gradient_weights_with_moment.mem
        if self.bias:
            self.bias.map_write()
            self.gradient_bias_with_moment.map_write()
            self.gradient_bias_with_moment.mem *= self.gradient_moment_bias
            self.gradient_bias_with_moment.mem += data[1]
            self.bias.mem += self.gradient_bias_with_moment.mem

    def drop_slave(self, slave):
        pass

    def accumulate_gradient_f(self, accumulated_gradient, gradient):
        if accumulated_gradient and self.accumulate_gradient:
            accumulated_gradient[:] = (
                gradient * self.acc_alpha +
                (self.acc_beta * accumulated_gradient if self.acc_beta else 0))

            gradient *= self.gd_beta
            gradient += self.gd_alpha * accumulated_gradient

        return gradient

    @staticmethod
    def numpy_gradient_step(weight, gradient, lr, factor_l12, l1_vs_l2,
                            factor_ortho=0, weights_transposed=False):
        gradient = gradient.copy()
        gradient += factor_l12 * ((1.0 - l1_vs_l2) * weight +
                                  0.5 * l1_vs_l2 * numpy.sign(weight))
        if factor_ortho:
            col_sums = (reshape_transposed(weight).sum(axis=1)
                        if weights_transposed else weight.sum(axis=0))
            for i, row in enumerate(gradient):
                row += (col_sums - weight[i]) * factor_ortho / weight.shape[0]
        gradient *= lr
        return gradient

    def run(self):
        self.gradient_changed = True
        super(GradientDescentBase, self).run()
        self.ocl_set_const_args = False


class NNWorkflow(AcceleratedWorkflow):
    """Base class for neural network workflow.

    Attributes:
        repeater: Repeater unit.
        loader: loader.Loader unit.
        forwards: list of the forward propagation (Forward) units.
        evaluator: evaluator.* unit.
        decision: decision.Decision unit.
        gds: list of the gradient descent units.
    """
    def __init__(self, workflow, **kwargs):
        super(NNWorkflow, self).__init__(workflow, **kwargs)
        self._repeater = Repeater(self)
        self._loader = None
        self._forwards = []
        self._evaluator = None
        self._decision = None
        self._gds = []

    @property
    def repeater(self):
        return self._repeater

    @property
    def forwards(self):
        return self._forwards

    @property
    def gds(self):
        return self._gds

    @property
    def loader(self):
        if self._loader is None:
            raise AttributeError(
                "No loader unit currently exists. You must set it first.")
        return self._loader

    @loader.setter
    def loader(self, value):
        if not isinstance(value, (Loader, Avatar)):
            raise TypeError(
                "Loader must be an instance of veles.loader.Loader")
        self._loader = value

    @property
    def decision(self):
        if self._decision is None:
            raise AttributeError(
                "No decision unit currently exists. You must set it first.")
        return self._decision

    @decision.setter
    def decision(self, value):
        if not isinstance(value, DecisionBase):
            raise TypeError(
                "Decision must be an instance of veles.znicz.decision."
                "DecisionBase")
        self._decision = value

    @property
    def evaluator(self):
        if self._evaluator is None:
            raise AttributeError(
                "No evaluator unit currently exists. You must set it first.")
        return self._evaluator

    @evaluator.setter
    def evaluator(self, value):
        if value is None:
            raise ValueError("Evaluator may not be None")
        if not isinstance(value, EvaluatorBase) and (
                not hasattr(value, "output") or "input" not in value.demanded):
            raise TypeError(
                "Evaluator must be either an instance of veles.znicz.evaluator"
                ".EvaluatorBase or demand \"input\" and provide \"output\" "
                "(got %s)." % type(value))
        self._evaluator = value


class NNSnapshotterBase(SnapshotterBase):
    def __init__(self, workflow, **kwargs):
        super(NNSnapshotterBase, self).__init__(workflow, **kwargs)
        self.has_invalid_values = Bool(False)

    def _log_attr(self, unit, attr, logged):
        val = getattr(unit, attr, None)
        if val is None:
            return
        mem = getattr(val, "mem", None)
        if mem is None:
            return
        val.map_read()
        if id(mem) not in logged:
            self.has_invalid_values <<= bool(
                numpy.count_nonzero(numpy.isnan(mem)) or
                numpy.count_nonzero(numpy.isinf(mem)))
            args = ("%s: %s: min max avg: %.6f %.6f %.6f%s",
                    unit.__class__.__name__, attr,
                    mem.min(), mem.max(), numpy.average(mem),
                    " has invalid values" if self.has_invalid_values else "")
            if self.has_invalid_values:
                self.error(*args)
            else:
                self.info(*args)
            logged.add(id(mem))

    def run(self):
        if not super(NNSnapshotterBase, self).run():
            return
        logged = set()
        for u in self.workflow.start_point.dependent_units():
            for attr in ("input", "weights", "bias", "output",
                         "err_output", "err_input"):
                self._log_attr(u, attr, logged)
        del logged
        _, dt = timeit(gc.collect)
        if dt > 1.0:
            self.warning("gc.collect() took %.1f sec", dt)


class NNSnapshotterToFile(NNSnapshotterBase, SnapshotterToFile):
    MAPPING = "nnfile"


class NNSnapshotterToDB(NNSnapshotterBase, SnapshotterToDB):
    MAPPING = "nnodbc"
