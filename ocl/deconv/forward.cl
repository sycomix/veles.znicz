#include "defines.cl"
#include "highlight.cl"

#ifndef WEIGHTS_TRANSPOSED
#error "WEIGHTS_TRANSPOSED should be defined"
#endif

#if (!(USE_HITS > 0)) && ((KX % SLIDE_X != 0) || (KY % SLIDE_Y != 0))
#error "Incorrect SLIDE"
#endif


#include "conv_common.cl"


/// @brief Does deconvolution.
/// @param input output of the corresponding convolutional layer.
/// @param weights weights.
/// @param output deconvolution of input.
/// @param hits number of the summations to this point of output.
/// @details output = input * weights.
__kernel __attribute__((reqd_work_group_size(BLOCK_SIZE, BLOCK_SIZE, 1)))
void feed_layer(__global const dtype      /* IN */    *input,
                __global const dtype      /* IN */    *weights,
                __global dtype           /* OUT */    *output
#if USE_HITS > 0
                , __global volatile int   /* IN */    *hits
#endif
                ) {

  #define A_WIDTH (BATCH * KERNELS_PER_SAMPLE)
  #define B_WIDTH ELEMENTS_PER_KERNEL
  #define AB_COMMON N_KERNELS

  #define A input
  #define B weights

  #if WEIGHTS_TRANSPOSED <= 0
  #define B_COL
  #endif

  #define STORE_OUTPUT "deconv/forward.store_output.cl"
  #include "matrix_multiplication.cl"

  #if WEIGHTS_TRANSPOSED <= 0
  #undef B_COL
  #endif

  #undef A_WIDTH
  #undef B_WIDTH
  #undef AB_COMMON

  #undef A
  #undef B
}


#if USE_HITS > 0
__kernel
void apply_hits(__global dtype    /* IN, OUT */    *output,
                __global const int     /* IN */    *hits) {
  int idx = get_global_id(0);
  int n = hits[idx];
  output[idx] /= n ? n : 1;
}

KERNEL_CLEAR(clear_hits, int)
#endif


KERNEL_CLEAR(clear_output, dtype)
