'''
Test that pickling can be used as a general "save network state" mechanism.
'''
import cPickle as pickle

from brian2 import *

def test_continue_simulation():
    '''
    Test that the CUBA network simulation can be continued after pickling
    '''
    taum = 20 * ms
    taue = 5 * ms
    taui = 10 * ms
    Vt = -50 * mV
    Vr = -60 * mV
    El = -49 * mV

    eqs = '''
    dv/dt  = (ge+gi-(v-El))/taum : volt (unless refractory)
    dge/dt = -ge/taue : volt (unless refractory)
    dgi/dt = -gi/taui : volt (unless refractory)
    '''

    P = NeuronGroup(4000, eqs, threshold='v>Vt', reset='v=Vr', refractory=5 * ms)
    P.v = Vr
    P.ge = 0 * mV
    P.gi = 0 * mV

    we = (60 * 0.27 / 10) * mV # excitatory synaptic weight (voltage)
    wi = (-20 * 4.5 / 10) * mV # inhibitory synaptic weight
    Ce = Synapses(P, P, 'w:1', pre='ge += we')
    Ci = Synapses(P, P, 'w:1', pre='gi += wi')
    Ce.connect('i<3200', p=0.02)
    Ci.connect('i>=3200', p=0.02)
    P.v = Vr + rand(len(P)) * (Vt - Vr)
    net = Network(P, Ce, Ci)
    net.run(100*ms)

    #pickle the network
    pickled = pickle.dumps(net)

    # continue the run and record spikes
    spike_mon = SpikeMonitor(P)
    net.add(spike_mon)
    net.run(100*ms)
    i, t = spike_mon.it_

    # Unpickle the network and continue from the old state
    net = pickle.loads(pickled)
    spike_mon = SpikeMonitor(P)
    net.add(spike_mon)
    net.run(100*ms)
    i, t = spike_mon.it_

    # Compare the old and new results

if __name__ == '__main__':
    test_continue_simulation()
