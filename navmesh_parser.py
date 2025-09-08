import io
import struct
from typing import Tuple, Union
from wizwalker import XYZ

type_format_dict = {
    "char": "<c",
    "signed char": "<b",
    "unsigned char": "<B",
    "bool": "?",
    "short": "<h",
    "unsigned short": "<H",
    "int": "<i",
    "unsigned int": "<I",
    "long": "<l",
    "unsigned long": "<L",
    "long long": "<q",
    "unsigned long long": "<Q",
    "float": "<f",
    "double": "<d",
}

class TypedBytes(io.BytesIO):
    def split(self, index: int) -> Tuple["TypedBytes", "TypedBytes"]:
        self.seek(0)
        buffer = self.read(index)
        return type(self)(buffer), type(self)(self.read())

    def read_typed(self, type_name: str):
        type_format = type_format_dict[type_name]
        size = struct.calcsize(type_format)
        data = self.read(size)
        return struct.unpack(type_format, data)[0]

def parse_nav_data(file_data: Union[bytes, TypedBytes]):
    if isinstance(file_data, bytes):
        file_data = TypedBytes(file_data)
    vertex_count = file_data.read_typed("short")
    vertex_max = file_data.read_typed("short")
    # unknown bytes
    file_data.read_typed("short")
    vertices = []
    idx = 0
    while idx <= vertex_max - 1:
        x = file_data.read_typed("float")
        y = file_data.read_typed("float")
        z = file_data.read_typed("float")
        vertices.append(XYZ(x, y, z))
        vertex_index = file_data.read_typed("short")
        if vertex_index != idx:
            vertices.pop()
            vertex_max -= 1
        else:
            idx += 1
    edge_count = file_data.read_typed("int")
    edges = []
    for idx in range(edge_count):
        start = file_data.read_typed("short")
        stop = file_data.read_typed("short")
        edges.append((start, stop))
    return vertices, edges