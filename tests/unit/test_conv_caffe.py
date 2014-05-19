"""
Created on May 19, 2014

Unit test for convolutional layer forward propagation, compared to CAFFE data.

Copyright (c) 2013 Samsung Electronics Co., Ltd.
"""

import logging
from veles import opencl
import unittest
from veles.formats import Vector

import veles.znicz.conv as conv
from veles.tests.dummy_workflow import DummyWorkflow

from scipy.signal import correlate2d

import numpy as np


class TestConvCaffe(unittest.TestCase):
    def setUp(self):
        self.workflow = DummyWorkflow()
        self.device = opencl.Device()

    def tearDown(self):
        pass

    def _read_array(self, array_name, lines, shape):
        """
            Shape=(n_pics, size_by_i, size_by_j, n_chans)
        """
        n_pics, height, width, n_chans = shape

        out_array = np.zeros(shape=shape, dtype=np.float64)

        cur_line = None
        for i, line in enumerate(lines):
            line = line.replace("\n", "")
            if line == array_name:
                cur_line = i + 1

        assert cur_line is not None
        assert cur_line < len(lines)

        for cur_pic in range(n_pics):
            nibbles = lines[cur_line].split(":")
            assert nibbles[0] == "num"
            assert int(nibbles[1]) == cur_pic
            cur_line += 1
            for cur_chan in range(n_chans):
                nibbles = lines[cur_line].split(":")
                assert nibbles[0] == "channels"
                assert int(nibbles[1]) == cur_chan
                cur_line += 1

                for i in range(height):
                    data = [float(x) for x in lines[cur_line].split("\t")]
                    cur_line += 1

                    for j in range(width):
                        out_array[cur_pic, i, j, cur_chan] = data[j]
        return out_array

    #TODO: prettify

    def test_caffe_version(self, data_path="/home/agolovizin/convtest.txt"):
        in_file = open(data_path, 'r')
        lines = in_file.readlines()
        in_file.close()

        kernel_size = 5
        padding_size = 2

        bottom = self._read_array("bottom", lines=lines, shape=(2, 32, 32, 3))
        weights = self._read_array("weights", lines=lines, shape=(2, 5, 5, 3))
        top = self._read_array("top", lines=lines, shape=(2, 32, 32, 2))

        fwd_conv = conv.Conv(self.workflow, kx=kernel_size, ky=kernel_size,
                             padding=(padding_size, padding_size,
                                      padding_size, padding_size),
                             sliding=(1, 1),
                             n_kernels=2)

        fwd_conv.input = Vector()
        fwd_conv.input.v = bottom

        #UNCOMMENT TO SEE CAFFEE DATA
#        print("bottom shape:", bottom.shape)
#        print(bottom)
#        print("weights shape:", weights.shape)
#        print(weights)
#        print("top shape:", top.shape)
#        print(top)

        fwd_conv.initialize(self.device)
        fwd_conv.weights.map_invalidate()
        fwd_conv.weights.v[:] = weights.reshape(2, 75)[:]
        fwd_conv.bias.map_invalidate()
        fwd_conv.bias.v[:] = 0
        fwd_conv.run()

        logging.info("COMPARED TO CAFFE DATA:")
        fwd_conv.output.map_read()

        logging.info("Veles top shape:" + str(fwd_conv.output.v.shape))
        print(fwd_conv.output.v - top)

        logging.info("COMPARED TO HANDMADE CORRELATION:")
        hand_conv_out = np.zeros(shape=(2, 32, 32, 2), dtype=np.float64)

        for pic in range(2):
            for color_chan in range(3):
                for weight_id in range(2):
                    correlation = correlate2d(
                        bottom[pic, :, :, color_chan],
                        weights[weight_id, :, :, color_chan], mode="same")
                    hand_conv_out[pic, :, :, weight_id] += correlation
        print(str(fwd_conv.output.v - hand_conv_out))

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info("CAFFE CONV TESTЪ")
    unittest.main()
