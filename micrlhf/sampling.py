import dataclasses
import random
from collections import OrderedDict
from functools import partial
from typing import Tuple, Union

import jax
import jax.numpy as jnp
import tiktoken
import transformers
from penzai import pz
from penzai.toolshed import jit_wrapper
from tqdm.auto import trange

from micrlhf.caching_llama import (LlamaKVCachingInputs, LlamaKVCachingState,
                                   LlamaKVCachingTransformer)
from micrlhf.llama import LlamaTransformer
from micrlhf.tokenizer import load_tokenizer


@partial(jax.jit, donate_argnums=(1, 3), static_argnums=(4,))
def sample_logits(logits, tokens, cache, key, do_sample=False):
    if do_sample:
        key_sample, key = jax.random.split(key)
        choices = pz.nx.nmap(lambda l: jax.random.categorical(key_sample, l))(
            logits.untag("batch", "vocabulary")).tag("batch").untag("seq")[cache.cache_end_index - 1]
    else:
        choices = logits.untag("vocabulary").argmax().untag("seq")[cache.cache_end_index - 1]
    tokens = pz.nx.nmap(lambda t, c: t.at[cache.cache_end_index].set(c))(tokens.untag("seq"), choices).tag("seq")
    return choices, tokens, key


@partial(jax.jit, donate_argnums=(1, 2, 3, 4), static_argnums=(7,))
def sample_step(llama_cached, advanced, tokens, cache, key, base_mask, initial_length, do_sample):
    batch_size = tokens.named_shape["batch"]
    max_seq_len = tokens.named_shape["seq"]
    inputs = LlamaKVCachingInputs(
        tokens=advanced[None].tag("seq"),
        positions=pz.nx.full({"batch": batch_size, "seq": 1}, cache.cache_end_index, jnp.int32),
        attention_mask=((pz.nx.wrap(cache.cache_end_index) >= pz.nx.arange("kv_seq", max_seq_len, dtype=jnp.int32))
                        & (base_mask | (pz.nx.arange("seq", max_seq_len, dtype=jnp.int32) >= initial_length)
                            ).untag("seq").tag("kv_seq"))[None].tag("seq"),
        sampling_state=cache
    )
    logits, cache = llama_cached(inputs)
    advanced, tokens, key = sample_logits(logits, tokens, cache, key, do_sample)
    return advanced, tokens, cache, key

def sample(llama: Union[LlamaTransformer, Tuple[LlamaKVCachingTransformer, LlamaKVCachingState]],
           tokenizer : tiktoken.Encoding | transformers.PreTrainedTokenizerBase,
            # TODO: multiple prompts and left padding
           prompt: str,
           batch_size: int = 4,
           max_seq_len: int = 64,
           pad_token_id: int = 128_020,
           do_sample: bool = False,
           return_model: bool = False):
    tokens = tokenizer.encode(prompt)
    initial_length = len(tokens)
    tokens = [tokens + [pad_token_id] * (max_seq_len - len(tokens))]
    tokens = jnp.asarray(tokens, dtype=jnp.int32)
    tokens = pz.nx.NamedArray(OrderedDict(batch=1, seq=max_seq_len), tokens)
    tokens = tokens.untag("batch").repeat(batch_size).tag("batch")
    if isinstance(llama, tuple):
        llama_cached, cache = llama
    else:
        llama_cached, cache = LlamaKVCachingTransformer.from_uncached(llama, max_seq_len, {"batch": batch_size})
    if return_model:
        llama_base = llama_cached
    if return_model:
        cache_base = cache
    llama_cached = jit_wrapper.Jitted(llama_cached)

    # prefill
    base_inputs = LlamaKVCachingInputs.from_basic_subsegments(tokens, cache)
    base_mask = tokens != pad_token_id
    base_inputs = dataclasses.replace(base_inputs,
                                      attention_mask=base_inputs.attention_mask & base_mask.untag("seq").tag("kv_seq")
                                      )
    logits, cache = llama_cached(base_inputs)
    cache = dataclasses.replace(cache, cache_end_index=initial_length)

    key = jax.random.key(random.randrange(0, 2**32))
    # generate
    advanced, tokens, key = sample_logits(logits, tokens, cache, key, do_sample=do_sample)

    for _ in (bar := trange(max_seq_len)):
        advanced, tokens, cache, key = sample_step(llama_cached, advanced, tokens, cache, key,
                                                   base_mask, initial_length, do_sample=do_sample)
        # bar.set_description(tokenizer.decode(tokens.untag("batch", "seq").data_array[0]))

    texts = [tokenizer.decode(sequence) for sequence in tokens.untag("batch", "seq").data_array]
    if return_model:
        return texts, (llama_base, cache_base)
    else:
        return texts


def main(
    filename = "models/phi-3-16.gguf",
    # prompt = "<|begin_of_text|><|start_header_id|>user<|end_header_id|>Hello<|eot_id|><|start_header_id|>assistant<|end_header_id|>Hi,",
    prompt = "<s><|system|>You are an assistant.</s><|user|>Hello!</s><|assistant|>Hi,",
):
    tokenizer = transformers.AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-4k-instruct")
    llama = LlamaTransformer.from_pretrained(filename)
    print(sample(llama, tokenizer, prompt))


if __name__ == "__main__":
    main()
