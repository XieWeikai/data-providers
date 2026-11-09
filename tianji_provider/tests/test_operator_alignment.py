"""The provider's clock and filter policy stays independent of the native kernels."""
from fractions import Fraction

import numpy as np
import pytest
from scipy.interpolate import interp1d
from scipy.signal import resample_poly

from tianji_provider.alignment import interpolate, moving_bounds, output_grids, uniform_grid
from tianji_provider.provider import TianjiConfig


@pytest.mark.parametrize("fps", [3, 60, 240, 999])
def test_rational_endpoint_including_near_int64_max(fps):
    last = 2**63-1
    for duration in (0, 1_000_000_000//fps, (1_000_000_000+fps-1)//fps, 1_000_000_000):
        start = last-duration
        expected = [start+(i*1_000_000_000+fps//2)//fps for i in range(duration*fps//1_000_000_000+1)]
        np.testing.assert_array_equal(uniform_grid(start,last,fps),expected)
    cfg=TianjiConfig(task='fixture',fps=3,wrench_fps=3*int(fps//3))
    grid,windows=output_grids(last-1_000_000_000,last,cfg)
    np.testing.assert_array_equal(grid,windows[:,-1])


def test_lowpass_matches_scipy_with_jitter_and_separate_outages():
    rng=np.random.default_rng(41)
    start=1_750_000_000_000_000_000
    t=start+np.cumsum(rng.integers(4_930_000,5_070_000,size=190,dtype=np.int64))
    t[100:]+=140_000_000
    v=rng.normal(size=(190,14))
    queries=[]; outputs=[]
    for run in (slice(0,100),slice(100,None)):
        times=t[run]; y=v[run]; q=times[2::4]+30_000
        x=(times-times[0]).astype(float)/1e9
        hz=round(1e9/np.median(np.diff(times)))
        regular=np.arange(int(np.floor(x[-1]*hz))+1)/hz
        pad=int(np.ceil(hz/50))
        y=np.pad(interp1d(x,y,axis=0)(regular),((pad,pad),(0,0)),mode='edge')
        ratio=Fraction(50,hz)
        f=resample_poly(y,ratio.numerator,ratio.denominator,axis=0,padtype='line')
        outputs.append(interp1d(np.arange(len(f))/50-pad/hz,f,axis=0)((q-times[0])/1e9))
        queries.append(q)
    result=interpolate(t,v,np.concatenate(queries),50,100_000_000)
    np.testing.assert_allclose(result,np.concatenate(outputs),rtol=2e-13,atol=2e-14)
    with pytest.raises(ValueError,match='unsupported gap'):
        interpolate(t,v,np.array([t[99]+100_000_000]),50,100_000_000)


def test_distinguish_stationary_from_disjoint_endpoint_departures():
    t=np.arange(31,dtype=np.int64)*50_000_000
    a=np.zeros((31,16))
    with pytest.raises(ValueError,match='No motion exceeds'):
        moving_bounds(t,a,.6,.01)
    a[7:24,0]=.5
    a[24:,0]=1.
    with pytest.raises(ValueError,match='fewer than two'):
        moving_bounds(t,a,.6,.01)
