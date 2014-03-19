#!/usr/bin/env python
# coding: latin-1
"""
This is a Brian script implementing a benchmark described
in the following review paper:

Simulation of networks of spiking neurons: A review of tools and strategies (2007).
Brette, Rudolph, Carnevale, Hines, Beeman, Bower, Diesmann, Goodman, Harris, Zirpe,
Natschläger, Pecevski, Ermentrout, Djurfeldt, Lansner, Rochel, Vibert, Alvarez, Muller,
Davison, El Boustani and Destexhe.
Journal of Computational Neuroscience 23(3):349-98

Benchmark 1: random network of integrate-and-fire neurons with exponential synaptic conductances

Clock-driven implementation with Euler integration
(no spike time interpolation)

R. Brette - Dec 2007
--------------------------------------------------------------------------------------
Brian is a simulator for spiking neural networks written in Python, developed by
R. Brette and D. Goodman.
http://brian.di.ens.fr
"""
from brian2 import *
import time

standalone = False
if standalone:
    set_device('cpp_standalone')
else:
    brian_prefs.codegen.target = 'weave'

# Time constants
taum = 20 * msecond
taue = 5 * msecond
taui = 10 * msecond
# Reversal potentials
Ee = (0. + 60.) * mvolt
Ei = (-80. + 60.) * mvolt

start_time = time.time()
eqs = Equations('''
dv/dt = (-v+ge*(Ee-v)+gi*(Ei-v))*(1./taum) : volt (unless refractory)
dge/dt = -ge*(1./taue) : 1
dgi/dt = -gi*(1./taui) : 1
''')
# NB 1: conductances are in units of the leak conductance
# NB 2: multiplication is faster than division

P = NeuronGroup(4000, model=eqs, threshold='v>10*mV',
                reset='v=0*mvolt', refractory=5*ms, method='euler')
Pe = P[:3200]
Pi = P[3200:]
we = 6. / 10. # excitatory synaptic weight (voltage)
wi = 67. / 10. # inhibitory synaptic weight
Ce = Synapses(Pe, P, pre='ge+=%f' % we)
Ce.connect(True, p=0.02)
Ci = Synapses(Pi, P, pre='gi+=%f' % wi)
Ci.connect(True, p=0.02)

# Initialization
P.v = '(randn()*5 - 5)*mV'
P.ge = 'randn()*1.5 + 4'
P.gi = 'randn()*12 + 20'

# Record the number of spikes
Me = PopulationRateMonitor(Pe)
Mi = PopulationRateMonitor(Pi)
print 'excitatory synapses', len(Ce)
print 'inhibitory synapses', len(Ci)
print "Network construction time:", time.time() - start_time, "seconds"
print "Simulation running..."

start_time = time.time()
run(20 * second)
if standalone:
    device.build(compile_project=True, run_project=True)
else:
    duration = time.time() - start_time

    print "Simulation time:", duration, "seconds"
    print np.mean(Me.rate), "excitatory rate"
    print np.mean(Mi.rate), "inhibitory rate"

    print '\n', '*'*50
    print 'Synaptic update'
    print Ci.pre.codeobj.code
    print ''
    print 'State update'
    print P.state_updater.codeobj.code



