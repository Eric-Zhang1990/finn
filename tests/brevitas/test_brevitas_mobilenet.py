from PIL import Image
import numpy as np
import brevitas.onnx as bo

import torch
from finn.util.basic import make_build_dir
from finn.util.test import get_test_model_trained
from finn.core.modelwrapper import ModelWrapper
from finn.transformation.infer_shapes import InferShapes
from finn.transformation.fold_constants import FoldConstants
from finn.transformation.infer_datatypes import InferDataTypes
from finn.transformation.general import GiveUniqueNodeNames
import finn.core.onnx_exec as oxe


def test_brevitas_mobilenet():
    export_onnx_path = make_build_dir("test_brevitas_")
    finn_onnx = export_onnx_path + "quant_mobilenet_v1_4b.onnx"
    mobilenet = get_test_model_trained("mobilenet", 4, 4)
    # get single image as input
    img = Image.open("/workspace/finn/tests/brevitas/king_charles.jpg")
    img = img.resize((224, 224))
    img = np.asarray(img).copy().astype(np.int32)
    img = img.transpose(2, 0, 1)
    # our network is trained with BGR instead of RGB images,
    # so we need to invert the order of channels in the channel axis:
    img = img[::-1, :, :].copy()
    # finally, we need to subtract the mean per-channel pixel intensity
    # since this is how this network has been trained
    img[0] = img[0] - 104
    img[1] = img[1] - 117
    img[2] = img[2] - 123
    img = img.reshape(1, 3, 224, 224)
    input_tensor = torch.from_numpy(img).float()
    assert input_tensor.shape == (1, 3, 224, 224)
    # do forward pass in PyTorch/Brevitas
    expected = mobilenet.forward(input_tensor).detach().numpy()
    # expected = expected.flatten()
    # winner_inds_top5 = np.argsort(expected)[-5:]
    # winner_ind = winner_inds_top5[-1]
    # winner_prob = expected[winner_ind]
    # assert winner_prob != 0
    bo.export_finn_onnx(mobilenet, (1, 3, 224, 224), finn_onnx, input_t=input_tensor)
    model = ModelWrapper(finn_onnx)
    model = model.transform(InferShapes())
    model = model.transform(FoldConstants())
    model = model.transform(InferShapes())
    model = model.transform(InferDataTypes())
    model = model.transform(GiveUniqueNodeNames())
    model.save("quant_mobilenet_v1_4b.onnx")
    idict = {model.graph.input[0].name: img.astype(np.float32)}
    odict = oxe.execute_onnx(model, idict, True)
    produced = odict[model.graph.output[0].name]
    assert (produced == expected).all()
