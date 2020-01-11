import bz2
import csv
import zlib
from io import FileIO
from tempfile import SpooledTemporaryFile
from typing import AsyncGenerator, Optional, Iterable

import pandas as pd
import pyarrow.orc as orc
import zstd


async def open_gzip_block(fd: FileIO):
    decomp = zlib.decompressobj(zlib.MAX_WBITS | 16)

    async for buf in fd:
        yield decomp.decompress(buf)

    yield decomp.flush()


async def open_bzip2_block(fd: FileIO):
    decomp = bz2.BZ2Decompressor()

    async for buf in fd:
        yield decomp.decompress(buf)


async def open_zstd_block(fd: FileIO):
    dctx = zstd.ZstdDecompressor()
    dobj = dctx.decompressobj()

    async for buf in fd:
        yield dobj.decompress(buf)


async def open_gzip(fd: FileIO):
    decomp = zlib.decompressobj(zlib.MAX_WBITS | 16)

    out_buf = b''
    async for buf in fd:
        out_buf += decomp.decompress(buf)
        lines = out_buf.splitlines(True)
        for line in lines[:-1]:
            yield line

        if lines:
            out_buf = lines[-1]
        else:
            out_buf = b''

    out_buf += decomp.flush()
    for line in out_buf.splitlines(True):
        yield line


async def open_bzip2(fd: FileIO):
    decomp = bz2.BZ2Decompressor()

    out_buf = b''
    async for buf in fd:
        out_buf += decomp.decompress(buf)
        lines = out_buf.splitlines(True)
        for line in lines[:-1]:
            yield line

        if lines:
            out_buf = lines[-1]
        else:
            out_buf = b''

        if lines:
            yield lines[-1]


async def open_zstd(fd: FileIO):
    dctx = zstd.ZstdDecompressor()
    decomp = dctx.decompressobj()

    out_buf = b''
    async for buf in fd:
        out_buf += decomp.decompress(buf)
        lines = out_buf.splitlines(True)
        for line in lines[:-1]:
            yield line

        if lines:
            out_buf = lines[-1]
        else:
            out_buf = b''

    if lines:
        yield lines[-1]


class PandasBatches(object):
    def __init__(self, batches, schema):
        self.batches = batches
        for b in batches:
            print(type(b))

    def __iter__(self):
        return self.batches


async def open_arrow_pandas(encoder, fd: FileIO, buffer_memory: int = 1048576):
    with SpooledTemporaryFile(mode='w+b', max_size=buffer_memory) as wfd:
        async for buf in fd:
            wfd.write(buf)

        wfd.flush()
        wfd.seek(0)
        return encoder.read_table(wfd)


async def open_parquet_pandas(fd: FileIO, buffer_memory: int = 1048576):
    with SpooledTemporaryFile(mode='w+b', max_size=buffer_memory) as wfd:
        async for buf in fd:
            wfd.write(buf)

        wfd.flush()
        wfd.seek(0)
        return pd.read_parquet(wfd, engine='pyarrow')


async def open_orc_pandas(fd: FileIO, buffer_memory: int = 1048576):
    return await open_arrow_pandas(orc, fd, buffer_memory)


async def open_parquet(fd: FileIO, buffer_memory: int = 1048576):
    for batch in await open_parquet_pandas(fd, buffer_memory):
        for row in batch.itertuples(index=False):
            yield b','.join((value if isinstance(value, bytes) else str(value).encode('utf-8') for value in row[1:]))


async def open_orc(fd: FileIO, buffer_memory: int = 1048576):
    async for batch in open_parquet_pandas(fd, buffer_memory):
        for row in batch.itertuples(index=False):
            yield b','.join((value if isinstance(value, bytes) else str(value).encode('utf-8') for value in row[1:]))


async def reader_columnar(fd: AsyncGenerator, encoding: str, compression: Optional[str] = None,
                          buffer_size: int = 16384, ignore_header=True):
    if encoding == 'parquet':
        table = await open_parquet_pandas(fd)
    elif encoding == 'orc':
        table = await open_orc_pandas(fd)
    else:
        raise ValueError('Invalid encoding {encoding}'.format(encoding=encoding))

    if not ignore_header:
        yield(list(table.columns.values))

    for row in table.itertuples(index=False):
        yield [col.decode('utf-8') if isinstance(col, bytes) else str(col) for col in row]

async def reader_compressed(fd: AsyncGenerator, compression: Optional[str] = None,
                            buffer_size: int = 16384, dialect: str = 'excel', *args, **kwargs):
    if compression is None:
        lines = fd
    elif compression == 'gzip':
        lines = open_gzip(fd)
    elif compression == 'bzip2':
        lines = open_bzip2(fd)
    elif compression == 'zstd':
        lines = open_zstd(fd)
    else:
        raise ValueError('Invalid compression {compression}'.format(compression=compression))

    buffer = []
    buffer_len = 0
    async for line in lines:
        buffer_len += len(line)
        buffer.append(line.decode('utf-8'))
        if buffer_len >= buffer_size:
            for row in csv.reader(buffer, dialect, *args, **kwargs):
                yield row
            buffer = []
            buffer_len = 0

    if buffer:
        for row in csv.reader(buffer, dialect, *args, **kwargs):
            yield row


async def open_compressed(fd: AsyncGenerator, compression: Optional[str] = None, buffer_size: int = 16384):
    if compression is None:
        lines = fd
    elif compression == 'gzip':
        lines = open_gzip(fd)
    elif compression == 'bzip2':
        lines = open_bzip2(fd)
    elif compression == 'zstd':
        lines = open_zstd(fd)
    else:
        raise ValueError('Invalid compression {compression}'.format(compression=compression))

    async for line in lines:
        yield line


class AsyncReader(object):
    def __init__(self, aiter: Optional[AsyncGenerator], ignore_header: bool = True, header: Iterable[str] = None):
        self._aiter = aiter
        self._ignore_header = ignore_header
        self._header = header

    async def header(self):
        if not self._ignore_header and not self._header:
            self._header = self._aiter.__anext__()

        return await self._header

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def __aiter__(self):
        return self

    def __anext__(self):
        if not self._ignore_header and not self._header:
            self._header = self._aiter.__anext__()
        return self._aiter.__anext__()

    async def close(self):
        pass


def reader(fd: AsyncGenerator, encoding: Optional[str] = None, compression: Optional[str] = None,
                 buffer_size: int = 16384,
                 dialect: str = 'excel', ignore_header=True, *args, **kwargs):
    if encoding:
        return AsyncReader(reader_columnar(fd, encoding, compression, buffer_size, ignore_header=ignore_header), ignore_header=ignore_header)
    else:
        return AsyncReader(reader_compressed(fd, compression, buffer_size), ignore_header=ignore_header)

# async def run():
#     async with aiofiles.open('samples/volumes/localstack/s3/bucket/test/nation.dict.parquet.bz2', 'rb') as fd:
#         async for row in reader(fd, encoding='parquet', compression='bzip2'):
#             print(row)
#
#
# asyncio.run(run())
