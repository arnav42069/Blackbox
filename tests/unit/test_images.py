import io

import pytest
from PIL import Image, PngImagePlugin

from blackbox.storage.images import normalize


def test_normalize_strips_metadata_and_downsizes():
    info = PngImagePlugin.PngInfo()
    info.add_text('secret', 'private metadata')
    b=io.BytesIO()
    Image.new('RGB', (3000, 1000), 'white').save(b, format='PNG', pnginfo=info)
    data, mime, dims = normalize(b.getvalue())
    assert dims == (1600, 533) and mime == 'image/png'
    with Image.open(io.BytesIO(data)) as image:
        assert not image.info


@pytest.mark.parametrize('data', [b'<svg onload=alert(1)>', b'fake PNG', b'x'*(8*1024*1024+1)])
def test_invalid_image(data):
    with pytest.raises(ValueError):
        normalize(data)


def test_dimension_cap():
    b=io.BytesIO()
    Image.new('RGB',(4097,1)).save(b,format='PNG')
    with pytest.raises(ValueError):
        normalize(b.getvalue())
