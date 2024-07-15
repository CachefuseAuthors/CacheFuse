# CacheFuse: 

This is an anonymous code repo for [You Only Prefill Once: Fusing Cached Knowledge for Large Language Model Serving with CacheFuse]() (in submission, EuroSys'25). The current implementation is based on [vLLM](https://github.com/vllm-project/vllm/tree/main).

## Installation
To install CacheFuse depenencies
```
git clone git@github.com:CachefuseAuthors/CacheFuse.git
cd CacheFuse/vllm_fuse
pip install -e .
cd ..
```


## Example run
### Run LLM inference w./w.o. CacheBlend with a single script
```
python example/blend.py
```
## References
