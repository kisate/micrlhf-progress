import dataclasses
from collections import OrderedDict
from typing import Dict, Literal, Optional, Tuple

import jax
import jax.numpy as jnp
import jax.sharding as jshard
import numpy as np
from jaxtyping import Array, Float16, Int8
from penzai import pz
from penzai.toolshed import sharding_util


def make_param(uninitialized_param: pz.nn.UninitializedParameter,
               quant_type: Literal["fp32", "q8_0", "q4_k", "q6_k"],
               tensor_data: Tuple[np.array],
               shape: Tuple[int],
               mesh: Optional[jshard.Mesh] = None,
               axis_name_to_mesh_name: Optional[Dict[str, str]] = None,
               ) -> pz.nn.Parameter:
    name = uninitialized_param.name 
    named_shape = uninitialized_param.value_structure.named_shape
    dtype = uninitialized_param.value_structure.dtype
    
    assert np.prod(shape) == np.prod(list(named_shape.values()))
    
    if ".attn.query" in name or ".attn.key" in name:
        new_data = []
        for d in tensor_data:
            # llama.cpp does rotary differently, i think
            head_dim = named_shape["projection"]
            embed_dim = named_shape["embedding"]
            n_heads = np.prod(list(named_shape.values())) // head_dim // embed_dim
            d = d \
                .reshape(n_heads, head_dim // 2, 2, -1, *d.shape[1:]) \
                    .swapaxes(1, 2) \
                        .reshape(d.shape)  # taking the mayakovsky pill
            new_data.append(d)
        tensor_data = new_data
    if quant_type == "fp32":
        dequantized = tensor_data[0]
    elif quant_type == "q8_0":
        return Int8Parameter(
            name=name,
            value_structure=uninitialized_param.value_structure,
            scale=jax.device_put(tensor_data[0]),
            quants=jax.device_put(tensor_data[1]),
            shape=shape[::-1],
            transpose=not name.endswith(".embeddings")
        )
    else:
        raise NotImplementedError(f"Quantization type {quant_type} not implemented")

    dequantized = dequantized.reshape(shape[::-1])
    if not name.endswith(".embeddings"):
        dequantized = dequantized.T  # for jax
    dequantized = jnp.asarray(dequantized.astype(dtype)).reshape(named_shape.values())
    dequantized = pz.nx.NamedArray(OrderedDict(named_shape), dequantized)
    # TODO make a custom ParameterLike for quantized parameters
    return pz.nn.Parameter(
        dequantized,
        name,
    )
    # sharding_util.name_to_name_device_put(name, mesh, axis_name_to_mesh_name=axis_name_to_mesh_name)


# not actually a parameter - doesn't inherit from pz.nn.Parameter
@pz.pytree_dataclass
class QuantizedParameter(pz.Struct):
    name: str = dataclasses.field(metadata={"pytree_node": False})
    value_structure: pz.chk.ArraySpec
    shape: Tuple[int] = dataclasses.field(metadata={"pytree_node": False})
    transpose: bool = dataclasses.field(metadata={"pytree_node": False})
    
    def dequantize(self):
        raise NotImplementedError("Abstract quantized parameter doesn't have a dequantize method")
    
    @property
    def value(self):
        dequantized = self.dequantize()
        dequantized = dequantized.reshape(self.shape)
        if self.transpose:
            dequantized = dequantized.T
        named_shape = self.value_structure.named_shape
        dtype = self.value_structure.dtype
        dequantized = jnp.asarray(dequantized.astype(dtype)).reshape(named_shape.values())
        dequantized = pz.nx.NamedArray(OrderedDict(named_shape), dequantized)
        return dequantized


@pz.pytree_dataclass(has_implicitly_inherited_fields=True)
class Int8Parameter(QuantizedParameter):
    scale: Float16[Array, "blocks 1"]
    quants: Int8[Array, "blocks 32"]

    def dequantize(self):
        dtype = self.value_structure.dtype
        return self.scale.astype(dtype) * self.quants
