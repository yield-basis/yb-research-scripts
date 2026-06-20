#!/usr/bin/env python3

import pylab
import lzma
import io

from datetime import datetime
import numpy as np


STEP = 500


with lzma.open('detailed-output.npz.xz', 'rb') as f:
    data = np.load(io.BytesIO(f.read()))
    t_raw = data['t'][::STEP]
    p = data['close'][::STEP]
    ps = data['price_scale'][::STEP]


t = [datetime.fromtimestamp(int(ts)) for ts in t_raw]

pylab.plot(t, p, c="black", label="Spot price")
pylab.plot(t, ps, c="gray", label="price_scale")
pylab.xlabel('Time')
pylab.ylabel('Price')
pylab.xticks(rotation=45, ha='right')
pylab.legend()
pylab.grid()
pylab.tight_layout()
pylab.show()
