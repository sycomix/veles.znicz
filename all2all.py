"""
Created on Mar 20, 2013

All2All units.

@author: Kazantsev Alexey <a.kazantsev@samsung.com>
"""
import units
import formats
import numpy
import pyopencl
import time
import rnd
import config


class All2All(units.OpenCLUnit):
    """All2All with linear activation f(x) = x.

    Should be assigned before initialize():
        input

    Updates after run():
        output

    Creates within initialize():
        weights
        bias

    Attributes:
        input: input as Batch.
        output: output as Batch.
        weights: weights as Vector.
        bias: bias as Vector.
        output_shape: shape of the output layer.
        weights_amplitude: amplitude of the random distribution of weights.
        rand: rnd.Rand() object.
        krn_: OpenCL kernel.
        s_activation: activation define for OpenCL source.
    """
    def __init__(self, output_shape=None, device=None, weights_amplitude=0.05,
                 rand=rnd.default, unpickling=0):
        super(All2All, self).__init__(unpickling=unpickling, device=device)
        self.krn_ = None
        if unpickling:
            return
        self.cl_sources["%s/forward.cl" % (config.cl_dir, )] = ""
        self.input = None  # formats.Batch(device)
        self.output = formats.Batch(device)
        self.weights = formats.Vector(device)
        self.bias = formats.Vector(device)
        self.output_shape = output_shape
        self.weights_amplitude = weights_amplitude
        self.rand = rand
        self.s_activation = "ACTIVATION_LINEAR"

    def initialize(self):
        if self.weights_amplitude == None:
            self.weights_amplitude = 9.0 / (self.input.batch.size //
                                            self.input.batch.shape[0])
        n_weights = self.input.batch.size // self.input.batch.shape[0] * \
                    numpy.prod(self.output_shape)
        if self.weights.v == None or self.weights.v.size != n_weights:
            self.weights.v = numpy.zeros([n_weights],
                                         dtype=config.dtypes[config.dtype])
            self.rand.fill(self.weights.v, -self.weights_amplitude,
                           self.weights_amplitude)
            # Reshape weights as a transposed matrix:
            self.weights.v = self.weights.v.\
                reshape([numpy.prod(self.output_shape),
                         self.input.batch.size // self.input.batch.shape[0]])
            self.weights.v_ = None
        if self.bias.v == None or \
           self.bias.v.size != numpy.prod(self.output_shape):
            self.bias.v = numpy.zeros([numpy.prod(self.output_shape)],
                                      dtype=config.dtypes[config.dtype])
            self.rand.fill(self.bias.v, -self.weights_amplitude,
                           self.weights_amplitude)
            self.bias.v_ = None

        output_size = self.input.batch.shape[0] * numpy.prod(self.output_shape)
        if self.output.batch == None or self.output.batch.size != output_size:
            self.output.batch = numpy.zeros([self.input.batch.shape[0],
                                             numpy.prod(self.output_shape)],
                                            dtype=config.dtypes[config.dtype])
            self.output.batch_ = None

        self.input.initialize(self.device)
        self.output.initialize(self.device)
        self.weights.initialize(self.device)
        self.bias.initialize(self.device)

        if not self.device:
            return

        if self.krn_ == None:
            output_size = self.output.aligned_.size // \
                          self.output.aligned_.shape[0]
            defines = ("%s\n"
                       "#define %s\n"
                       "#define BLOCK_SIZE %d\n"
                       "#define H %d\n"
                       "#define Y %d\n"
                       "#define Y_REAL %d\n"
                       "#define BATCH %d\n\n" %
                       (config.cl_defines[config.dtype], self.s_activation,
                        self.device.info.BLOCK_SIZE[config.dtype],
                        self.weights.aligned_.size // output_size, output_size,
                        self.output.batch.size // self.output.batch.shape[0],
                        self.output.aligned_.shape[0]))
            s = defines
            for src, define in self.cl_sources.items():
                s += "\n" + define + "\n"
                fin = open(src, "r")
                s += fin.read()
                fin.close()
            global this_dir
            fin = open("%s/matrix_multiplication.cl" % (config.cl_dir, ), "r")
            s_mx_mul = fin.read()
            fin.close()
            s = s.replace("MX_MUL", s_mx_mul)
            fout = open("%s/feed_%d_%d.cl" % (config.cache_dir,
                self.input.batch.size // self.input.batch.shape[0],
                self.output.batch.size // self.output.batch.shape[0]), "w")
            fout.write(s)
            fout.close()

            self.prg_ = pyopencl.Program(self.device.context_, s).build()

            self.krn_ = pyopencl.Kernel(self.prg_, "FEED_LAYER")
            self.krn_.set_arg(0, self.input.batch_)
            self.krn_.set_arg(1, self.weights.v_)
            self.krn_.set_arg(2, self.output.batch_)
            self.krn_.set_arg(3, self.bias.v_)

    def print_times(self, t_start):
        """Show some statistics.
        """
        if not __debug__:
            # self.log().info("%s within %.2f sec: %d_%d" % \
            #      (self.__class__.__name__, time.time() - t_start, \
            #       self.input.batch.size // self.input.batch.shape[0], \
            #       self.output.batch.size // self.output.batch.shape[0]))
            return
        y = self.output.batch
        self.output.sync()
        self.weights.sync()
        self.log().info("%s: %d samples with %d weights in %.2f sec "
            "(min,avg,max,sum):\ty=%.6f,%.4f,%.2f,%.2f" %
            (self.__class__.__name__.replace("All2All", ""), y.shape[0],
             self.weights.v.size, time.time() - t_start,
             numpy.fabs(y).min(), numpy.average(numpy.fabs(y)),
             numpy.fabs(y).max(), y.sum()))
        self.show_weights()

    def gpu_run(self):
        """Forward propagation from batch on GPU.
        """
        self.input.sync(formats.GPU)
        self.weights.sync(formats.GPU)
        self.bias.sync(formats.GPU)
        output_size = int(self.output.aligned_.size //
                          self.output.aligned_.shape[0])
        global_size = [output_size, self.output.aligned_.shape[0]]
        local_size = [self.device.info.BLOCK_SIZE[config.dtype],
                      self.device.info.BLOCK_SIZE[config.dtype]]
        event = pyopencl.enqueue_nd_range_kernel(self.device.queue_, self.krn_,
                                                 global_size, local_size)
        event.wait()
        self.output.update(formats.GPU)

    def cpu_run(self):
        """Forward propagation from batch on CPU only.
        """
        self.input.sync()
        self.weights.sync()
        self.bias.sync()
        a = self.input.batch.reshape([self.input.batch.shape[0],
            self.input.batch.size // self.input.batch.shape[0]])
        b = self.weights.v.transpose()
        numpy.dot(a, b, self.output.batch)
        self.output.batch[:] += self.bias.v
        self.output.update()

    def run(self):
        t1 = time.time()
        retval = super(All2All, self).run()
        if retval:
            return retval
        self.print_times(t1)


class All2AllTanh(All2All):
    """All2All with scaled tanh() activation f(x) = 1.7159 * tanh(0.6666 * x).
    """
    def initialize(self):
        self.s_activation = "ACTIVATION_TANH"
        return super(All2AllTanh, self).initialize()

    def cpu_run(self):
        """Forward propagation from batch on CPU only.
        """
        retval = super(All2AllTanh, self).cpu_run()
        if retval:
            return retval
        self.output.sync()
        self.output.batch *= 0.6666
        numpy.tanh(self.output.batch, self.output.batch)
        self.output.batch *= 1.7159
        self.output.update()


class All2AllSoftmax(All2All):
    """All2All with linear activation and softmax normalization.

    Should be assigned before initialize():

    Updates after run():
        max_idx

    Creates within initialize():
        max_idx

    Attributes:
        krn_sm_: kernel for softmax activation calculation.
        max_idx: indexes of element with maximum value for each sample.
    """
    def __init__(self, output_shape=None, device=None, weights_amplitude=0.05,
                 rand=rnd.default, unpickling=0):
        super(All2AllSoftmax, self).__init__(output_shape=output_shape,
            device=device, weights_amplitude=weights_amplitude, rand=rand,
            unpickling=unpickling)
        self.krn_sm_ = None
        if unpickling:
            return
        self.max_idx = formats.Batch()

    def initialize(self):
        itype = config.get_itype_from_size(numpy.prod(self.output_shape))
        global this_dir
        self.cl_sources["%s/softmax.cl" % (config.cl_dir, )] = (
            "#define itype %s" % (itype, ))
        retval = super(All2AllSoftmax, self).initialize()
        if retval:
            return retval

        if self.max_idx.batch == None or \
           self.max_idx.batch.size != self.output.batch.shape[0]:
            self.max_idx.batch = numpy.zeros([self.output.batch.shape[0]],
                dtype=config.itypes[itype])
            self.max_idx.batch_ = None

        self.max_idx.initialize(self.device)

        if not self.device:
            return

        self.krn_sm_ = pyopencl.Kernel(self.prg_, "apply_exp")
        self.krn_sm_.set_arg(0, self.output.batch_)
        self.krn_sm_.set_arg(1, self.max_idx.batch_)

    def cpu_apply_exp(self):
        self.output.sync()
        if __debug__:
            s = []
            a = numpy.sort(self.output.batch.reshape(self.output.batch.size))
            for i in range(a.size - 1, a.size - 11, -1):
                s.append("%.2f" % (a[i],))
            self.log().debug("Softmax Wx+b: ", ", ".join(s),
                             ", %.2f" % (a[0], ))
        for i in range(0, self.output.batch.shape[0]):
            sample = self.output.batch[i]
            im = sample.argmax()
            self.max_idx[i] = im
            m = sample[im]
            sample -= m
            numpy.exp(sample, sample)
            smm = sample.sum()
            sample /= smm
        self.output.update()
        self.max_idx.update()

    def gpu_apply_exp(self):
        self.output.sync(formats.GPU)
        global_size = [self.device.info.BLOCK_SIZE[config.dtype],
                       self.output.aligned_.shape[0]]
        local_size = [self.device.info.BLOCK_SIZE[config.dtype],
                      self.device.info.BLOCK_SIZE[config.dtype]]
        event = pyopencl.enqueue_nd_range_kernel(self.device.queue_,
                                                 self.krn_sm_,
                                                 global_size, local_size)
        event.wait()
        self.output.update(formats.GPU)
        self.max_idx.update(formats.GPU)

    def cpu_run(self):
        """Forward propagation from batch on CPU only.
        """
        retval = super(All2AllSoftmax, self).cpu_run()
        if retval:
            return retval
        self.cpu_apply_exp()

    def gpu_run(self):
        """Forward propagation from batch on GPU.
        """
        retval = super(All2AllSoftmax, self).gpu_run()
        if retval:
            return retval
        self.gpu_apply_exp()
