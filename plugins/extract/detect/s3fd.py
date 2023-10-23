#!/usr/bin/env python3
""" S3FD Face detection plugin
https://arxiv.org/abs/1708.05237

Adapted from S3FD Port in FAN:
https://github.com/1adrianb/face-alignment
"""
import logging
from typing import List, Optional, Tuple

from scipy.special import logsumexp
import numpy as np

from lib.model.session import KSession
from lib.utils import get_backend
from ._base import BatchType, Detector

if get_backend() == "amd":
    import keras
    from keras import backend as K
    from keras.layers import Concatenate, Conv2D, Input, Maximum, MaxPooling2D, ZeroPadding2D
    from plaidml.tile import Value as Tensor  # pylint:disable=import-error
else:
    # Ignore linting errors from Tensorflow's thoroughly broken import system
    from tensorflow import keras
    from tensorflow.keras import backend as K  # pylint:disable=import-error
    from tensorflow.keras.layers import (  # pylint:disable=no-name-in-module,import-error
        Concatenate, Conv2D, Input, Maximum, MaxPooling2D, ZeroPadding2D)
    from tensorflow import Tensor

logger = logging.getLogger(__name__)


class Detect(Detector):
    """ S3FD detector for face recognition """
    def __init__(self, **kwargs) -> None:
        git_model_id = 11
        model_filename = "s3fd_keras_v2.h5"
        super().__init__(git_model_id=git_model_id, model_filename=model_filename, **kwargs)
        self.name = "S3FD"
        self.input_size = 640
        self.vram = 4112
        self.vram_warnings = 1024  # Will run at this with warnings
        self.vram_per_batch = 208
        self.batchsize = self.config["batch-size"]

    def init_model(self) -> None:
        """ Initialize S3FD Model"""
        assert isinstance(self.model_path, str)
        confidence = self.config["confidence"] / 100
        model_kwargs = dict(custom_objects=dict(L2Norm=L2Norm, SliceO2K=SliceO2K))
        self.model = S3fd(self.model_path,
                          model_kwargs,
                          self.config["allow_growth"],
                          self._exclude_gpus,
                          confidence)

    def process_input(self, batch: BatchType) -> None:
        """ Compile the detection image(s) for prediction """
        assert isinstance(self.model, S3fd)
        batch.feed = self.model.prepare_batch(np.array(batch.image))

    def predict(self, feed: np.ndarray) -> np.ndarray:
        """ Run model to get predictions """
        assert isinstance(self.model, S3fd)
        predictions = self.model.predict(feed)
        assert isinstance(predictions, list)
        return self.model.finalize_predictions(predictions)

    def process_output(self, batch) -> None:
        """ Compile found faces for output """
        return


################################################################################
# CUSTOM KERAS LAYERS
################################################################################
class L2Norm(keras.layers.Layer):
    """ L2 Normalization layer for S3FD.

    Parameters
    ----------
    n_channels: int
        The number of channels to normalize
    scale: float, optional
        The scaling for initial weights. Default: `1.0`
    """
    def __init__(self, n_channels: int, scale: float = 1.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._n_channels = n_channels
        self._scale = scale
        self.w = self.add_weight("l2norm",  # pylint:disable=invalid-name
                                 (self._n_channels, ),
                                 trainable=True,
                                 initializer=keras.initializers.Constant(value=self._scale),
                                 dtype="float32")

    def call(self, inputs: Tensor) -> Tensor:    # pylint:disable=arguments-differ
        """ Call the L2 Normalization Layer.

        Parameters
        ----------
        inputs: tensor
            The input to the L2 Normalization Layer

        Returns
        -------
        tensor:
            The output from the L2 Normalization Layer
        """
        norm = K.sqrt(K.sum(K.pow(inputs, 2), axis=-1, keepdims=True)) + 1e-10
        return inputs / norm * self.w

    def get_config(self) -> dict:
        """ Returns the config of the layer.

        Returns
        -------
        dict
            The configuration for the layer
        """
        config = super().get_config()
        config.update({"n_channels": self._n_channels,
                       "scale": self._scale})
        return config


class SliceO2K(keras.layers.Layer):
    """ Custom Keras Slice layer generated by onnx2keras. """
    def __init__(self,
                 starts: List[int],
                 ends: List[int],
                 axes: Optional[List[int]] = None,
                 steps: Optional[List[int]] = None,
                 **kwargs) -> None:
        self._starts = starts
        self._ends = ends
        self._axes = axes
        self._steps = steps
        super().__init__(**kwargs)

    def _get_slices(self, dimensions: int) -> List[Tuple[int, ...]]:
        """ Obtain slices for the given number of dimensions.

        Parameters
        ----------
        dimensions: int
            The number of dimensions to obtain slices for

        Returns
        -------
        list
            The slices for the given number of dimensions
        """
        axes = tuple(range(dimensions)) if self._axes is None else self._axes
        steps = (1,) * len(axes) if self._steps is None else self._steps
        assert len(axes) == len(steps) == len(self._starts) == len(self._ends)
        return list(zip(axes, self._starts, self._ends, steps))

    def compute_output_shape(self, input_shape: Tuple[int, ...]) -> Tuple[int, ...]:
        """Computes the output shape of the layer.

        Assumes that the layer will be built to match that input shape provided.

        Parameters
        ----------
        input_shape: tuple or list of tuples
            Shape tuple (tuple of integers) or list of shape tuples (one per output tensor of the
            layer). Shape tuples can include ``None`` for free dimensions, instead of an integer.

        Returns
        -------
        tuple
            An output shape tuple.
        """
        in_shape = list(input_shape)
        for a_x, start, end, steps in self._get_slices(len(in_shape)):
            size = in_shape[a_x]
            if a_x == 0:
                raise AttributeError("Can not slice batch axis.")
            if size is None:
                if start < 0 or end < 0:
                    raise AttributeError("Negative slices not supported on symbolic axes")
                logger.warning("Slicing symbolic axis might lead to problems.")
                in_shape[a_x] = (end - start) // steps
                continue
            if start < 0:
                start = size - start
            if end < 0:
                end = size - end
            in_shape[a_x] = (min(size, end) - start) // steps
        return tuple(in_shape)

    def call(self, inputs, **kwargs):    # pylint:disable=unused-argument,arguments-differ
        """This is where the layer's logic lives.

        Parameters
        ----------
        inputs: Input tensor, or list/tuple of input tensors.
            The input to the layer
        **kwargs: Additional keyword arguments.
            Required for parent class but unused
        Returns
        -------
        A tensor or list/tuple of tensors.
            The layer output
        """
        ax_map = {x[0]: slice(*x[1:]) for x in self._get_slices(K.ndim(inputs))}
        shape = K.int_shape(inputs)
        slices = [ax_map.get(a, slice(None)) for a in range(len(shape))]
        return inputs[tuple(slices)]

    def get_config(self) -> dict:
        """ Returns the config of the layer.

        Returns
        -------
        dict
            The configuration for the layer
        """
        config = super().get_config()
        config.update({"starts": self._starts,
                       "ends": self._ends,
                       "axes": self._axes,
                       "steps": self._steps})
        return config


class S3fd(KSession):
    """ Keras Network """
    def __init__(self,
                 model_path: str,
                 model_kwargs: dict,
                 allow_growth: bool,
                 exclude_gpus: Optional[List[int]],
                 confidence: float) -> None:
        logger.debug("Initializing: %s: (model_path: '%s', model_kwargs: %s, allow_growth: %s, "
                     "exclude_gpus: %s, confidence: %s)", self.__class__.__name__, model_path,
                     model_kwargs, allow_growth, exclude_gpus, confidence)
        super().__init__("S3FD",
                         model_path,
                         model_kwargs=model_kwargs,
                         allow_growth=allow_growth,
                         exclude_gpus=exclude_gpus)
        self.define_model(self.model_definition)
        self.load_model_weights()
        self.confidence = confidence
        self.average_img = np.array([104.0, 117.0, 123.0])
        logger.debug("Initialized: %s", self.__class__.__name__)

    def model_definition(self) -> Tuple[List[Tensor], List[Tensor]]:
        """ Keras S3FD Model Definition, adapted from FAN pytorch implementation. """
        input_ = Input(shape=(640, 640, 3))
        var_x = self.conv_block(input_, 64, 1, 2)
        var_x = MaxPooling2D(pool_size=2, strides=2)(var_x)

        var_x = self.conv_block(var_x, 128, 2, 2)
        var_x = MaxPooling2D(pool_size=2, strides=2)(var_x)

        var_x = self.conv_block(var_x, 256, 3, 3)
        f3_3 = var_x
        var_x = MaxPooling2D(pool_size=2, strides=2)(var_x)

        var_x = self.conv_block(var_x, 512, 4, 3)
        f4_3 = var_x
        var_x = MaxPooling2D(pool_size=2, strides=2)(var_x)

        var_x = self.conv_block(var_x, 512, 5, 3)
        f5_3 = var_x
        var_x = MaxPooling2D(pool_size=2, strides=2)(var_x)

        var_x = ZeroPadding2D(3)(var_x)
        var_x = Conv2D(1024, kernel_size=3, strides=1, activation="relu", name="fc6")(var_x)
        var_x = Conv2D(1024, kernel_size=1, strides=1, activation="relu", name="fc7")(var_x)
        ffc7 = var_x

        f6_2 = self.conv_up(var_x, 256, 6)
        f7_2 = self.conv_up(f6_2, 128, 7)

        f3_3 = L2Norm(256, scale=10, name="conv3_3_norm")(f3_3)
        f4_3 = L2Norm(512, scale=8, name="conv4_3_norm")(f4_3)
        f5_3 = L2Norm(512, scale=5, name="conv5_3_norm")(f5_3)

        f3_3 = ZeroPadding2D(1)(f3_3)
        cls1 = Conv2D(4, kernel_size=3, strides=1, name="conv3_3_norm_mbox_conf")(f3_3)
        reg1 = Conv2D(4, kernel_size=3, strides=1, name="conv3_3_norm_mbox_loc")(f3_3)

        f4_3 = ZeroPadding2D(1)(f4_3)
        cls2 = Conv2D(2, kernel_size=3, strides=1, name="conv4_3_norm_mbox_conf")(f4_3)
        reg2 = Conv2D(4, kernel_size=3, strides=1, name="conv4_3_norm_mbox_loc")(f4_3)

        f5_3 = ZeroPadding2D(1)(f5_3)
        cls3 = Conv2D(2, kernel_size=3, strides=1, name="conv5_3_norm_mbox_conf")(f5_3)
        reg3 = Conv2D(4, kernel_size=3, strides=1, name="conv5_3_norm_mbox_loc")(f5_3)

        ffc7 = ZeroPadding2D(1)(ffc7)
        cls4 = Conv2D(2, kernel_size=3, strides=1, name="fc7_mbox_conf")(ffc7)
        reg4 = Conv2D(4, kernel_size=3, strides=1, name="fc7_mbox_loc")(ffc7)

        f6_2 = ZeroPadding2D(1)(f6_2)
        cls5 = Conv2D(2, kernel_size=3, strides=1, name="conv6_2_mbox_conf")(f6_2)
        reg5 = Conv2D(4, kernel_size=3, strides=1, name="conv6_2_mbox_loc")(f6_2)

        f7_2 = ZeroPadding2D(1)(f7_2)
        cls6 = Conv2D(2, kernel_size=3, strides=1, name="conv7_2_mbox_conf")(f7_2)
        reg6 = Conv2D(4, kernel_size=3, strides=1, name="conv7_2_mbox_loc")(f7_2)

        # max-out background label
        chunks = [SliceO2K(starts=[0], ends=[1], axes=[3], steps=None)(cls1),
                  SliceO2K(starts=[1], ends=[2], axes=[3], steps=None)(cls1),
                  SliceO2K(starts=[2], ends=[3], axes=[3], steps=None)(cls1),
                  SliceO2K(starts=[3], ends=[4], axes=[3], steps=None)(cls1)]

        bmax = Maximum()([chunks[0], chunks[1], chunks[2]])
        cls1 = Concatenate()([bmax, chunks[3]])

        return [input_], [cls1, reg1, cls2, reg2, cls3, reg3, cls4, reg4, cls5, reg5, cls6, reg6]

    @classmethod
    def conv_block(cls, inputs: Tensor, filters: int, idx: int, recursions: int) -> Tensor:
        """ First round convolutions with zero padding added.

        Parameters
        ----------
        inputs: tensor
            The input tensor to the convolution block
        filters: int
            The number of filters
        idx: int
            The layer index for naming
        recursions: int
            The number of recursions of the block to perform

        Returns
        -------
        tensor
            The output tensor from the convolution block
        """
        name = f"conv{idx}"
        var_x = inputs
        for i in range(1, recursions + 1):
            rec_name = f"{name}_{i}"
            var_x = ZeroPadding2D(1, name=f"{rec_name}.zeropad")(var_x)
            var_x = Conv2D(filters,
                           kernel_size=3,
                           strides=1,
                           activation="relu",
                           name=rec_name)(var_x)
        return var_x

    @classmethod
    def conv_up(cls, inputs: Tensor, filters: int, idx: int) -> Tensor:
        """ Convolution up filter blocks with zero padding added.

        Parameters
        ----------
        inputs: tensor
            The input tensor to the convolution block
        filters: int
            The initial number of filters
        idx: int
            The layer index for naming

        Returns
        -------
        tensor
            The output tensor from the convolution block
        """
        name = f"conv{idx}"
        var_x = inputs
        for i in range(1, 3):
            rec_name = f"{name}_{i}"
            size = 1 if i == 1 else 3
            if i == 2:
                var_x = ZeroPadding2D(1, name=f"{rec_name}.zeropad")(var_x)
            var_x = Conv2D(filters * i,
                           kernel_size=size,
                           strides=i,
                           activation="relu",
                           name=rec_name)(var_x)
        return var_x

    def prepare_batch(self, batch: np.ndarray) -> np.ndarray:
        """ Prepare a batch for prediction.

        Normalizes the feed images.

        Parameters
        ----------
        batch: class:`numpy.ndarray`
            The batch to be fed to the model

        Returns
        -------
        class:`numpy.ndarray`
            The normalized images for feeding to the model
        """
        batch = batch - self.average_img
        return batch

    def finalize_predictions(self, bounding_boxes_scales: List[np.ndarray]) -> np.ndarray:
        """ Process the output from the model to obtain faces

        Parameters
        ----------
        bounding_boxes_scales: list
            The output predictions from the S3FD model
        """
        ret = []
        batch_size = range(bounding_boxes_scales[0].shape[0])
        for img in batch_size:
            bboxlist = [scale[img:img+1] for scale in bounding_boxes_scales]
            boxes = self._post_process(bboxlist)
            finallist = self._nms(boxes, 0.5)
            ret.append(finallist)
        return np.array(ret, dtype="object")

    def _post_process(self, bboxlist: List[np.ndarray]) -> np.ndarray:
        """ Perform post processing on output
            TODO: do this on the batch.
        """
        retval = []
        for i in range(len(bboxlist) // 2):
            bboxlist[i * 2] = self.softmax(bboxlist[i * 2], axis=3)
        for i in range(len(bboxlist) // 2):
            ocls, oreg = bboxlist[i * 2], bboxlist[i * 2 + 1]
            stride = 2 ** (i + 2)    # 4,8,16,32,64,128
            poss = zip(*np.where(ocls[:, :, :, 1] > 0.05))
            for _, hindex, windex in poss:
                axc, ayc = stride / 2 + windex * stride, stride / 2 + hindex * stride
                score = ocls[0, hindex, windex, 1]
                if score >= self.confidence:
                    loc = np.ascontiguousarray(oreg[0, hindex, windex, :]).reshape((1, 4))
                    priors = np.array([[axc / 1.0, ayc / 1.0, stride * 4 / 1.0, stride * 4 / 1.0]])
                    box = self.decode(loc, priors)
                    x_1, y_1, x_2, y_2 = box[0] * 1.0
                    retval.append([x_1, y_1, x_2, y_2, score])
        return np.array(retval) if retval else np.zeros((1, 5))

    @staticmethod
    def softmax(inp, axis: int) -> np.ndarray:
        """Compute softmax values for each sets of scores in x."""
        return np.exp(inp - logsumexp(inp, axis=axis, keepdims=True))

    @staticmethod
    def decode(location: np.ndarray, priors: np.ndarray) -> np.ndarray:
        """Decode locations from predictions using priors to undo the encoding we did for offset
        regression at train time.

        Parameters
        ----------
        location: tensor
            location predictions for location layers,
        priors: tensor
            Prior boxes in center-offset form.

        Returns
        -------
        :class:`numpy.ndarray`
            decoded bounding box predictions
        """
        variances = [0.1, 0.2]
        boxes = np.concatenate((priors[:, :2] + location[:, :2] * variances[0] * priors[:, 2:],
                                priors[:, 2:] * np.exp(location[:, 2:] * variances[1])), axis=1)
        boxes[:, :2] -= boxes[:, 2:] / 2
        boxes[:, 2:] += boxes[:, :2]
        return boxes

    @staticmethod
    def _nms(boxes: np.ndarray, threshold: float) -> np.ndarray:
        """ Perform Non-Maximum Suppression """
        retained_box_indices = []

        areas = (boxes[:, 2] - boxes[:, 0] + 1) * (boxes[:, 3] - boxes[:, 1] + 1)
        ranked_indices = boxes[:, 4].argsort()[::-1]
        while ranked_indices.size > 0:
            best_rest = ranked_indices[0], ranked_indices[1:]

            max_of_xy = np.maximum(boxes[best_rest[0], :2], boxes[best_rest[1], :2])
            min_of_xy = np.minimum(boxes[best_rest[0], 2:4], boxes[best_rest[1], 2:4])
            width_height = np.maximum(0, min_of_xy - max_of_xy + 1)
            intersection_areas = width_height[:, 0] * width_height[:, 1]
            iou = intersection_areas / (areas[best_rest[0]] +
                                        areas[best_rest[1]] - intersection_areas)

            overlapping_boxes = (iou > threshold).nonzero()[0]
            if len(overlapping_boxes) != 0:
                overlap_set = ranked_indices[overlapping_boxes + 1]
                vote = np.average(boxes[overlap_set, :4], axis=0, weights=boxes[overlap_set, 4])
                boxes[best_rest[0], :4] = vote
            retained_box_indices.append(best_rest[0])

            non_overlapping_boxes = (iou <= threshold).nonzero()[0]
            ranked_indices = ranked_indices[non_overlapping_boxes + 1]
        return boxes[retained_box_indices]
