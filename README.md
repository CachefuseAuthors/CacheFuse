# CacheFuse: 

This is an anonymous code repo for [You Only Prefill Once: Fusing Cached Knowledge for Large Language Model Serving with CacheFuse]() (in submission, EuroSys'25). The current implementation is based on [vLLM](https://github.com/vllm-project/vllm/tree/main).

## Installation
To install CacheFuse depenencies
```
git clone git@github.com:CachefuseAuthors/CacheFuse.git
ca cachefuse
pip install -e .
```


## Example run
### Run LLM inference with CacheFuse
Step 1: To independently generate the KV caches for multiple text segments
```
python examples/preprocess.py --text_path inputs/1.json --cache_path <PATH OF THE KV CACHE>
```


Step 2: To run LLM inference w/ CacheFuse
```
python examples/fuse_gen.py --cache_path <PATH OF THE KV CACHE>
```

### Run normal LLM inference
To run LLM inference w/o CacheFuse
```
python examples/normal_gen.py
```
## References
