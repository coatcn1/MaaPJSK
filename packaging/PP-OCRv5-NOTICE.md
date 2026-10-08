# PP-OCRv5 mobile recognition model

- Official model: https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_rec_onnx
- Upstream OCR project: https://github.com/PaddlePaddle/PaddleOCR
- License: Apache-2.0; full text is included in `licenses/PaddleOCR-Apache-2.0.txt`.
- `inference.onnx` SHA256: `da72dc72ca4dc220df0dfde68c1dedc31c58d3e76a25871122e5056227d50092`
- `inference.yml` SHA256: `5dfeb2777f6d0db8177d8128a8acfcf6e6276dc4ac73ea3bf0dc06d6a5e85d8e`

The model and character dictionary are used for local recognition in fixed game UI regions, including song identity, map/CM navigation and login notices. Runtime recognition does not access the network. Model binaries are downloaded separately during development and included only in verified release assets.
