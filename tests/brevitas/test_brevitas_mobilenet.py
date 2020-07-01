from PIL import Image
import numpy as np
import brevitas.onnx as bo

import torch
from torch.nn import Module, Sequential
from finn.util.basic import make_build_dir
from finn.util.test import get_test_model_trained
from finn.core.modelwrapper import ModelWrapper
from finn.transformation.infer_shapes import InferShapes
from finn.transformation.infer_data_layouts import InferDataLayouts
from finn.transformation.fold_constants import FoldConstants
from finn.transformation.infer_datatypes import InferDataTypes
from finn.transformation.general import (
    GiveReadableTensorNames,
    GiveUniqueNodeNames,
    GiveUniqueParameterTensors,
)
from finn.transformation.insert_preproc import InsertPreProcessing
from finn.transformation.double_to_single_float import DoubleToSingleFloat
from finn.transformation.streamline import Streamline
from finn.transformation.streamline.remove import RemoveIdentityOps
from finn.transformation.streamline.reorder import (
    MoveMulPastDWConv,
    MoveTransposePastScalarMul,
    MoveFlattenPastAffine,
    MoveFlattenPastTopK,
    MoveScalarMulPastMatMul,
)
from finn.transformation.streamline.collapse_repeated import CollapseRepeatedMul
from finn.transformation.change_datalayout import ChangeDataLayoutQuantAvgPool2d
from finn.transformation.lower_convs_to_matmul import LowerConvsToMatMul
import finn.transformation.streamline.absorb as absorb
from finn.transformation.insert_topk import InsertTopK
import finn.core.onnx_exec as oxe


class Normalize(Module):
    def __init__(self, mean, std, channels):
        super(Normalize, self).__init__()

        self.mean = mean
        self.std = std
        self.channels = channels

    def forward(self, x):
        x = x - torch.tensor(self.mean, device=x.device).reshape(1, self.channels, 1, 1)
        x = x / self.std
        return x


class ToTensor(Module):
    def __init__(self):
        super(ToTensor, self).__init__()

    def forward(self, x):
        x = x / 255
        return x


class PreProc(Module):
    def __init__(self, mean, std, channels):
        super(PreProc, self).__init__()
        self.features = Sequential()
        scaling = ToTensor()
        self.features.add_module("scaling", scaling)
        normalize = Normalize(mean, std, channels)
        self.features.add_module("normalize", normalize)

    def forward(self, x):
        return self.features(x)


def test_brevitas_mobilenet():
    # get single image as input and prepare image
    img = Image.open("/workspace/finn/tests/brevitas/king_charles.jpg")
    # resize smallest side of the image to 256 pixels and resize larger side
    # with same ratio
    ratio = 256 / min(img.size)
    width = int(img.size[0] * ratio)
    height = int(img.size[1] * ratio)
    img = img.resize((width, height))
    # crop central 224*224 window
    left = (width - 224) / 2
    top = (height - 224) / 2
    right = (width + 224) / 2
    bottom = (height + 224) / 2
    img = img.crop((left, top, right, bottom))
    # save image as numpy array and as torch tensor to enable testing in
    # brevitas/pytorch and finn and transpose from (H, W, C) to (C, H, W)
    img_np = np.asarray(img).copy().astype(np.float32).transpose(2, 0, 1)
    img_np = img_np.reshape(1, 3, 224, 224)
    img_torch = torch.from_numpy(img_np).float()

    # export preprocess
    export_onnx_path = make_build_dir("test_brevitas_mobilenet-v1_")
    preproc_onnx = export_onnx_path + "quant_mobilenet_v1_4b_preproc.onnx"
    mean = [0.485, 0.456, 0.406]
    std = 0.226
    ch = 3
    preproc = PreProc(mean, std, ch)
    bo.export_finn_onnx(preproc, (1, 3, 224, 224), preproc_onnx)
    preproc_model = ModelWrapper(preproc_onnx)
    preproc_model = preproc_model.transform(InferShapes())
    preproc_model = preproc_model.transform(GiveUniqueNodeNames())
    preproc_model = preproc_model.transform(GiveUniqueParameterTensors())
    preproc_model = preproc_model.transform(GiveReadableTensorNames())

    finn_onnx = export_onnx_path + "quant_mobilenet_v1_4b.onnx"
    mobilenet = get_test_model_trained("mobilenet", 4, 4)
    bo.export_finn_onnx(mobilenet, (1, 3, 224, 224), finn_onnx)

    # do forward pass in PyTorch/Brevitas
    input_tensor = preproc.forward(img_torch)
    expected = mobilenet.forward(input_tensor).detach().numpy()
    expected_topk = expected.flatten()
    expected_top5 = np.argsort(expected_topk)[-5:]
    expected_top5 = np.flip(expected_top5)
    expected_top5_prob = []
    for index in expected_top5:
        expected_top5_prob.append(expected_topk[index])
    model = ModelWrapper(finn_onnx)
    model = model.transform(InferShapes())
    model = model.transform(FoldConstants())
    model = model.transform(InsertTopK())
    # get initializer from Mul that will be absorbed into topk
    a0 = model.get_initializer(model.graph.node[-2].input[1])
    model = model.transform(absorb.AbsorbScalarMulIntoTopK())
    model = model.transform(InferShapes())
    model = model.transform(InferDataTypes())
    model = model.transform(InferDataLayouts())
    model = model.transform(GiveUniqueNodeNames())
    model = model.transform(GiveUniqueParameterTensors())
    model = model.transform(GiveReadableTensorNames())
    model.save("quant_mobilenet_v1_4b_wo_preproc.onnx")
    model = model.transform(InsertPreProcessing(preproc_model))
    model.save("quant_mobilenet_v1_4b.onnx")
    idict = {model.graph.input[0].name: img_np}
    odict = oxe.execute_onnx(model, idict, True)
    produced = odict[model.graph.output[0].name]
    produced_prob = odict["TopK_0_out0"] * a0
    assert (produced.flatten() == expected_top5).all()
    assert np.isclose(produced_prob.flatten(), expected_top5_prob).all()
    model = model.transform(Streamline())
    model = model.transform(DoubleToSingleFloat())
    model = model.transform(MoveMulPastDWConv())
    model = model.transform(absorb.AbsorbMulIntoMultiThreshold())
    model = model.transform(ChangeDataLayoutQuantAvgPool2d())
    model = model.transform(InferDataLayouts())
    model = model.transform(MoveTransposePastScalarMul())
    model = model.transform(absorb.AbsorbTransposeIntoFlatten())
    model = model.transform(MoveFlattenPastAffine())
    model = model.transform(MoveFlattenPastTopK())
    # model.save("after_move_flatten.onnx")
    model = model.transform(MoveScalarMulPastMatMul())
    # model.save("after_movescalarmul.onnx")
    model = model.transform(CollapseRepeatedMul())
    # model.save("after_collapse.onnx")
    model = model.transform(RemoveIdentityOps())
    model = model.transform(LowerConvsToMatMul())
    model = model.transform(absorb.AbsorbTransposeIntoMultiThreshold())
    model = model.transform(GiveUniqueNodeNames())
    model = model.transform(GiveReadableTensorNames())
    model = model.transform(InferDataTypes())
    model.save("quant_mobilenet_v1_4b_streamlined.onnx")
    odict_streamline = oxe.execute_onnx(model, idict, True)
    produced_streamline = odict_streamline[model.graph.output[0].name]
    produced_streamline_prob = odict_streamline["TopK_0_out0"] * a0
    assert (produced_streamline.flatten() == expected_top5).all()
    assert np.isclose(produced_streamline_prob.flatten(), expected_top5_prob).all()
