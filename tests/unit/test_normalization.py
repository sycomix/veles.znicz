#!/usr/bin/python3 -O
# encoding: utf-8

"""
Created on April 24, 2014

A unit test for local response normalization.
"""

import gc
import logging
import numpy
import unittest

from veles import backends
from veles.config import root
from veles.dummy import DummyWorkflow
from veles.memory import Vector
from veles.opencl_types import dtypes
from veles.znicz.normalization import LRNormalizerForward, LRNormalizerBackward


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self.device = backends.Device()
        self.dtype = dtypes[root.common.precision_type]

    def tearDown(self):
        gc.collect()
        del self.device

    def test_normalization_forward(self):
        fwd_normalizer = LRNormalizerForward(DummyWorkflow())
        fwd_normalizer.input = Vector()
        in_vector = numpy.zeros(shape=(3, 2, 5, 19), dtype=self.dtype)

        for i in range(5):
            in_vector[0, 0, i, :] = numpy.linspace(10, 50, 19) * (i + 1)
            in_vector[0, 1, i, :] = numpy.linspace(10, 50, 19) * (i + 1) + 1
            in_vector[1, 0, i, :] = numpy.linspace(10, 50, 19) * (i + 1) + 2
            in_vector[1, 1, i, :] = numpy.linspace(10, 50, 19) * (i + 1) + 3
            in_vector[2, 0, i, :] = numpy.linspace(10, 50, 19) * (i + 1) + 4
            in_vector[2, 1, i, :] = numpy.linspace(10, 50, 19) * (i + 1) + 5

        fwd_normalizer.input.mem = in_vector
        fwd_normalizer.initialize(device=self.device)

        fwd_normalizer.ocl_run()
        fwd_normalizer.output.map_read()
        ocl_result = numpy.copy(fwd_normalizer.output.mem)

        fwd_normalizer.cpu_run()
        fwd_normalizer.output.map_read()
        cpu_result = numpy.copy(fwd_normalizer.output.mem)

        max_delta = numpy.fabs(cpu_result - ocl_result).max()

        logging.info("FORWARD")
        self.assertLess(max_delta, 0.0001,
                        "Result differs by %.6f" % (max_delta))

        logging.info("FwdProp done.")

    def test_normalization_backward(self):

        h = numpy.zeros(shape=(2, 1, 5, 5), dtype=self.dtype)
        for i in range(5):
            h[0, 0, i, :] = numpy.linspace(10, 50, 5) * (i + 1)
            h[1, 0, i, :] = numpy.linspace(10, 50, 5) * (i + 1) + 1

        err_y = numpy.zeros(shape=(2, 1, 5, 5), dtype=self.dtype)
        for i in range(5):
            err_y[0, 0, i, :] = numpy.linspace(2, 10, 5) * (i + 1)
            err_y[1, 0, i, :] = numpy.linspace(2, 10, 5) * (i + 1) + 1

        back_normalizer = LRNormalizerBackward(DummyWorkflow(), n=3)
        back_normalizer.input = Vector()
        back_normalizer.err_output = Vector()

        back_normalizer.input.mem = h
        back_normalizer.err_output.mem = err_y

        back_normalizer.initialize(device=self.device)

        back_normalizer.cpu_run()
        cpu_result = back_normalizer.err_input.mem.copy()

        back_normalizer.err_input.map_invalidate()
        back_normalizer.err_input.mem[:] = 100

        back_normalizer.ocl_run()
        back_normalizer.err_input.map_read()
        ocl_result = back_normalizer.err_input.mem.copy()

        logging.info("BACK")

        max_delta = numpy.fabs(cpu_result - ocl_result).max()
        self.assertLess(max_delta, 0.0001,
                        "Result differs by %.6f" % (max_delta))

        logging.info("BackProp done.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info("Running LR normalizer test!")
    unittest.main()
