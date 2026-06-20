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
    usd = data['token0'][::STEP]
    coin = data['token1'][::STEP]


t = [datetime.fromtimestamp(int(ts)) for ts in t_raw]

usd_f = usd / (usd + coin * p)

pylab.plot(t, usd_f, c="black")
pylab.xlabel('Time')
pylab.ylabel('Fraction in USD')
pylab.xticks(rotation=45, ha='right')
pylab.grid()
pylab.tight_layout()
pylab.show()
