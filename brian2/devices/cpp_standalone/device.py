'''
Module implementing the C++ "standalone" device.
'''
import os
import shutil
import subprocess
import inspect
from collections import defaultdict

import numpy as np

from brian2.core.clocks import defaultclock
from brian2.core.network import Network
from brian2.devices.device import Device, all_devices
from brian2.core.variables import *
from brian2.synapses.synapses import Synapses
from brian2.utils.filetools import copy_directory, ensure_directory, in_directory
from brian2.utils.stringtools import word_substitute
from brian2.codegen.generators.cpp_generator import c_data_type
from brian2.units.fundamentalunits import Quantity, have_same_dimensions
from brian2.units import second
from brian2.utils.logger import get_logger

from .codeobject import CPPStandaloneCodeObject


__all__ = []

logger = get_logger(__name__)


def freeze(code, ns):
    # this is a bit of a hack, it should be passed to the template somehow
    for k, v in ns.items():
        if isinstance(v, (int, float)): # for the namespace provided for functions
            code = word_substitute(code, {k: str(v)})
        elif (isinstance(v, Variable) and not isinstance(v, AttributeVariable) and
              v.scalar and v.constant and v.read_only):
            value = v.get_value()
            if value < 0:
                string_value = '(%r)' % value
            else:
                string_value = '%r' % value
            code = word_substitute(code, {k: string_value})
    return code


class CPPWriter(object):
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.source_files = []
        self.header_files = []
        
    def write(self, filename, contents):
        logger.debug('Writing file %s:\n%s' % (filename, contents))
        if filename.lower().endswith('.cpp'):
            self.source_files.append(filename)
        elif filename.lower().endswith('.h'):
            self.header_files.append(filename)
        elif filename.endswith('.*'):
            self.write(filename[:-1]+'cpp', contents.cpp_file)
            self.write(filename[:-1]+'h', contents.h_file)
            return
        fullfilename = os.path.join(self.project_dir, filename)
        if os.path.exists(fullfilename):
            if open(fullfilename, 'r').read()==contents:
                return
        open(fullfilename, 'w').write(contents)
        
        
def invert_dict(x):
    return dict((v, k) for k, v in x.iteritems())


class CPPStandaloneDevice(Device):
    '''
    The `Device` used for C++ standalone simulations.
    '''
    def __init__(self):
        super(CPPStandaloneDevice, self).__init__()
        #: Dictionary mapping `ArrayVariable` objects to their globally
        #: unique name
        self.arrays = {}
        #: List of all dynamic arrays
        #: Dictionary mapping `DynamicArrayVariable` objects with 1 dimension to
        #: their globally unique name
        self.dynamic_arrays = {}
        #: Dictionary mapping `DynamicArrayVariable` objects with 2 dimensions
        #: to their globally unique name
        self.dynamic_arrays_2d = {}
        #: List of all arrays to be filled with zeros
        self.zero_arrays = []
        #: Dictionary of all arrays to be filled with numbers (mapping
        #: `ArrayVariable` objects to start value)
        self.arange_arrays = {}

        #: Whether the simulation has been run
        self.has_been_run = False

        #: Dict of all static saved arrays
        self.static_arrays = {}

        self.code_objects = {}
        self.main_queue = []
        self.report_func = ''
        self.synapses = []
        
        self.clocks = set([])
        
    def reinit(self):
        self.__init__()

    def insert_device_code(self, slot, code):
        '''
        Insert code directly into main.cpp
        '''
        if slot=='main':
            self.main_queue.append(('insert_code', code))
        else:
            logger.warn("Ignoring device code, unknown slot: %s, code: %s" % (slot, code))
            
    def static_array(self, name, arr):
        assert len(arr), 'length for %s: %d' % (name, len(arr))
        name = '_static_array_' + name
        basename = name
        i = 0
        while name in self.static_arrays:
            i += 1
            name = basename+'_'+str(i)
        self.static_arrays[name] = arr.copy()
        return name

    def get_array_name(self, var, access_data=True):
        '''
        Return a globally unique name for `var`.

        Parameters
        ----------
        access_data : bool, optional
            For `DynamicArrayVariable` objects, specifying `True` here means the
            name for the underlying data is returned. If specifying `False`,
            the name of object itself is returned (e.g. to allow resizing).
        '''
        if isinstance(var, DynamicArrayVariable):
            if access_data:
                return self.arrays[var]
            elif var.dimensions == 1:
                return self.dynamic_arrays[var]
            else:
                return self.dynamic_arrays_2d[var]
        elif isinstance(var, ArrayVariable):
            return self.arrays[var]
        else:
            raise TypeError(('Do not have a name for variable of type '
                             '%s') % type(var))

    def add_array(self, var):
        # Note that a dynamic array variable is added to both the arrays and
        # the _dynamic_array dictionary
        if isinstance(var, DynamicArrayVariable):
            name = '_dynamic_array_%s_%s' % (var.owner.name, var.name)
            if var.dimensions == 1:
                self.dynamic_arrays[var] = name
            elif var.dimensions == 2:
                self.dynamic_arrays_2d[var] = name
            else:
                raise AssertionError(('Did not expect a dynamic array with %d '
                                      'dimensions.') % var.dimensions)

        name = '_array_%s_%s' % (var.owner.name, var.name)
        self.arrays[var] = name

    def init_with_zeros(self, var):
        self.zero_arrays.append(var)

    def init_with_arange(self, var, start):
        self.arange_arrays[var] = start

    def init_with_array(self, var, arr):
        array_name = self.get_array_name(var, access_data=False)
        # treat the array as a static array
        self.static_arrays[array_name] = arr.astype(var.dtype)

    def fill_with_array(self, var, arr):
        arr = np.asarray(arr)
        if arr.shape == ():
            arr = np.repeat(arr, var.size)
        # Using the std::vector instead of a pointer to the underlying
        # data for dynamic arrays is fast enough here and it saves us some
        # additional work to set up the pointer
        array_name = self.get_array_name(var, access_data=False)
        static_array_name = self.static_array(array_name, arr)
        self.main_queue.append(('set_by_array', (array_name,
                                                 static_array_name)))

    def variableview_set_with_index_array(self, variableview, item,
                                          value, check_units):
        if isinstance(item, slice) and item == slice(None):
            item = 'True'
        value = Quantity(value)

        if value.size == 1 and item == 'True':  # set the whole array to a scalar value
            if have_same_dimensions(value, 1):
                # Avoid a representation as "Quantity(...)" or "array(...)"
                value = float(value)
            variableview.set_with_expression_conditional(cond=item,
                                                         code=repr(value),
                                                         check_units=check_units)
        # Simple case where we don't have to do any indexing
        elif (item == 'True' and variableview.var_index == '_idx'):
            self.fill_with_array(variableview.variable, value)
        else:
            # We have to calculate indices. This will not work for synaptic
            # variables
            try:
                indices = variableview.indexing.calc_indices(item)
            except NotImplementedError:
                raise NotImplementedError(('Cannot set variable "%s" this way in '
                                           'standalone, try using string '
                                           'expressions.') % variableview.name)
            # Using the std::vector instead of a pointer to the underlying
            # data for dynamic arrays is fast enough here and it saves us some
            # additional work to set up the pointer
            arrayname = self.get_array_name(variableview.variable,
                                            access_data=False)
            staticarrayname_index = self.static_array('_index_'+arrayname,
                                                      indices)
            staticarrayname_value = self.static_array('_value_'+arrayname,
                                                      value)
            self.main_queue.append(('set_array_by_array', (arrayname,
                                                           staticarrayname_index,
                                                           staticarrayname_value)))

    def get_value(self, var, access_data=True):
        # Usually, we cannot retrieve the values of state variables in
        # standalone scripts since their values might depend on the evaluation
        # of expressions at runtime. For constant, read-only arrays that have
        # been explicitly initialized (static arrays) or aranges (e.g. the
        # neuronal indices) we can, however
        if var.constant and var.read_only:
            array_name = self.get_array_name(var, access_data=False)
            if array_name in self.static_arrays:
                return self.static_arrays[array_name]
            elif var in self.arange_arrays:
                return np.arange(0, var.size) + self.arange_arrays[var]
            else:
                raise AssertionError(('Variable %s is constant and read-only '
                                      ' but uninitialized'))
        else:
            # After the network has been run, we can retrieve the values from
            # disk
            if self.has_been_run:
                dtype = var.dtype
                array_name = array_name = self.get_array_name(var,
                                                              access_data=False)
                fname = os.path.join(self.project_dir, 'results',
                                     array_name)
                with open(fname, 'rb') as f:
                    data = np.fromfile(f, dtype=dtype)
                # This is a bit of an heuristic, but our 2d dynamic arrays are
                # only expanding in one dimension, we assume here that the
                # other dimension has size 0 at the beginning
                if isinstance(var.size, tuple) and len(var.size) == 2:
                    if var.size[0] * var.size[1] == len(data):
                        return data.reshape(var.size)
                    elif var.size[0] == 0:
                        return data.reshape((-1, var.size[1]))
                    elif var.size[0] == 0:
                        return data.reshape((var.size[1], -1))
                    else:
                        raise IndexError(('Do not now how to deal with 2d '
                                          'array of size %s, the array on disk '
                                          'has length %d') % (str(var.size),
                                                              len(data)))

                return data
            raise NotImplementedError('Cannot retrieve the values of state '
                                      'variables in standalone code before the '
                                      'simulation has been run.')


    def variableview_get_subexpression_with_index_array(self, variableview, item):
        raise NotImplementedError(('Cannot evaluate subexpressions in '
                                   'standalone scripts.'))


    def variableview_get_with_expression(self, variableview, code, level=0,
                                         run_namespace=None):
        raise NotImplementedError('Cannot retrieve the values of state '
                                  'variables with string expressions in '
                                  'standalone scripts.')

    def code_object_class(self, codeobj_class=None):
        if codeobj_class is not None:
            raise ValueError("Cannot specify codeobj_class for C++ standalone device.")
        return CPPStandaloneCodeObject

    def code_object(self, owner, name, abstract_code, variables, template_name,
                    variable_indices, codeobj_class=None, template_kwds=None,
                    override_conditional_write=None):
        codeobj = super(CPPStandaloneDevice, self).code_object(owner, name, abstract_code, variables,
                                                               template_name, variable_indices,
                                                               codeobj_class=codeobj_class,
                                                               template_kwds=template_kwds,
                                                               override_conditional_write=override_conditional_write,
                                                               )
        self.code_objects[codeobj.name] = codeobj
        return codeobj

    def build(self, project_dir='output', compile_project=True, run_project=False, debug=True,
              with_output=True, native=True,
              additional_source_files=None, additional_header_files=None,
              main_includes=None, run_includes=None,
              run_args=None,
              ):
        '''
        Build the project
        
        TODO: more details
        
        Parameters
        ----------
        project_dir : str
            The output directory to write the project to, any existing files will be overwritten.
        compile_project : bool
            Whether or not to attempt to compile the project using GNU make.
        run_project : bool
            Whether or not to attempt to run the built project if it successfully builds.
        debug : bool
            Whether to compile in debug mode.
        with_output : bool
            Whether or not to show the ``stdout`` of the built program when run.
        native : bool
            Whether or not to compile natively using the ``--march=native`` gcc option.
        additional_source_files : list of str
            A list of additional ``.cpp`` files to include in the build.
        additional_header_files : list of str
            A list of additional ``.h`` files to include in the build.
        main_includes : list of str
            A list of additional header files to include in ``main.cpp``.
        run_includes : list of str
            A list of additional header files to include in ``run.cpp``.
        '''
        
        if additional_source_files is None:
            additional_source_files = []
        if additional_header_files is None:
            additional_header_files = []
        if main_includes is None:
            main_includes = []
        if run_includes is None:
            run_includes = []
        if run_args is None:
            run_args = []
        self.project_dir = project_dir
        ensure_directory(project_dir)
        for d in ['code_objects', 'results', 'static_arrays']:
            ensure_directory(os.path.join(project_dir, d))
            
        writer = CPPWriter(project_dir)
            
        logger.debug("Writing C++ standalone project to directory "+os.path.normpath(project_dir))

        arange_arrays = sorted([(var, start)
                                for var, start in self.arange_arrays.iteritems()],
                               key=lambda (var, start): var.name)

        # # Find np arrays in the namespaces and convert them into static
        # # arrays. Hopefully they are correctly used in the code: For example,
        # # this works for the namespaces for functions with C++ (e.g. TimedArray
        # # treats it as a C array) but does not work in places that are
        # # implicitly vectorized (state updaters, resets, etc.). But arrays
        # # shouldn't be used there anyway.
        for code_object in self.code_objects.itervalues():
            for name, value in code_object.variables.iteritems():
                if isinstance(value, np.ndarray):
                    self.static_arrays[name] = value

        # write the static arrays
        logger.debug("static arrays: "+str(sorted(self.static_arrays.keys())))
        static_array_specs = []
        for name, arr in sorted(self.static_arrays.items()):
            arr.tofile(os.path.join(project_dir, 'static_arrays', name))
            static_array_specs.append((name, c_data_type(arr.dtype), arr.size, name))

        # Write the global objects
        networks = [net() for net in Network.__instances__() if net().name!='_fake_network']
        synapses = [S() for S in Synapses.__instances__()]
        arr_tmp = CPPStandaloneCodeObject.templater.objects(
                        None, None,
                        array_specs=self.arrays,
                        dynamic_array_specs=self.dynamic_arrays,
                        dynamic_array_2d_specs=self.dynamic_arrays_2d,
                        zero_arrays=self.zero_arrays,
                        arange_arrays=arange_arrays,
                        synapses=synapses,
                        clocks=self.clocks,
                        static_array_specs=static_array_specs,
                        networks=networks,
                        )
        writer.write('objects.*', arr_tmp)

        main_lines = []
        procedures = [('', main_lines)]
        runfuncs = {}
        for func, args in self.main_queue:
            if func=='run_code_object':
                codeobj, = args
                main_lines.append('_run_%s();' % codeobj.name)
            elif func=='run_network':
                net, netcode = args
                main_lines.extend(netcode)
            elif func=='set_by_array':
                arrayname, staticarrayname = args
                code = '''
                for(int i=0; i<_num_{staticarrayname}; i++)
                {{
                    {arrayname}[i] = {staticarrayname}[i];
                }}
                '''.format(arrayname=arrayname, staticarrayname=staticarrayname)
                main_lines.extend(code.split('\n'))
            elif func=='set_array_by_array':
                arrayname, staticarrayname_index, staticarrayname_value = args
                code = '''
                for(int i=0; i<_num_{staticarrayname_index}; i++)
                {{
                    {arrayname}[{staticarrayname_index}[i]] = {staticarrayname_value}[i];
                }}
                '''.format(arrayname=arrayname, staticarrayname_index=staticarrayname_index,
                           staticarrayname_value=staticarrayname_value)
                main_lines.extend(code.split('\n'))
            elif func=='insert_code':
                main_lines.append(args)
            elif func=='start_run_func':
                name, include_in_parent = args
                if include_in_parent:
                    main_lines.append('%s();' % name)
                main_lines = []
                procedures.append((name, main_lines))
            elif func=='end_run_func':
                name, include_in_parent = args
                name, main_lines = procedures.pop(-1)
                runfuncs[name] = main_lines
                name, main_lines = procedures[-1]
            else:
                raise NotImplementedError("Unknown main queue function type "+func)

        # generate the finalisations
        for codeobj in self.code_objects.itervalues():
            if hasattr(codeobj.code, 'main_finalise'):
                main_lines.append(codeobj.code.main_finalise)

        # Generate data for non-constant values
        code_object_defs = defaultdict(list)
        for codeobj in self.code_objects.itervalues():
            lines = []
            for k, v in codeobj.variables.iteritems():
                if isinstance(v, AttributeVariable):
                    # We assume all attributes are implemented as property-like methods
                    line = 'const {c_type} {varname} = {objname}.{attrname}();'
                    lines.append(line.format(c_type=c_data_type(v.dtype), varname=k, objname=v.obj.name,
                                             attrname=v.attribute))
                elif isinstance(v, ArrayVariable):
                    try:
                        if isinstance(v, DynamicArrayVariable):
                            if v.dimensions == 1:
                                dyn_array_name = self.dynamic_arrays[v]
                                array_name = self.arrays[v]
                                line = '{c_type}* const {array_name} = &{dyn_array_name}[0];'
                                line = line.format(c_type=c_data_type(v.dtype), array_name=array_name,
                                                   dyn_array_name=dyn_array_name)
                                lines.append(line)
                                line = 'const int _num{k} = {dyn_array_name}.size();'
                                line = line.format(k=k, dyn_array_name=dyn_array_name)
                                lines.append(line)
                        else:
                            lines.append('const int _num%s = %s;' % (k, v.size))
                    except TypeError:
                        pass
            for line in lines:
                # Sometimes an array is referred to by to different keys in our
                # dictionary -- make sure to never add a line twice
                if not line in code_object_defs[codeobj.name]:
                    code_object_defs[codeobj.name].append(line)

        # Generate the code objects
        for codeobj in self.code_objects.itervalues():
            ns = codeobj.variables
            # TODO: fix these freeze/CONSTANTS hacks somehow - they work but not elegant.
            code = freeze(codeobj.code.cpp_file, ns)
            code = code.replace('%CONSTANTS%', '\n'.join(code_object_defs[codeobj.name]))
            code = '#include "objects.h"\n'+code
            
            writer.write('code_objects/'+codeobj.name+'.cpp', code)
            writer.write('code_objects/'+codeobj.name+'.h', codeobj.code.h_file)
                    
        # The code_objects are passed in the right order to run them because they were
        # sorted by the Network object. To support multiple clocks we'll need to be
        # smarter about that.
        main_tmp = CPPStandaloneCodeObject.templater.main(None, None,
                                                          main_lines=main_lines,
                                                          code_objects=self.code_objects.values(),
                                                          report_func=self.report_func,
                                                          dt=float(defaultclock.dt),
                                                          additional_headers=main_includes,
                                                          )
        writer.write('main.cpp', main_tmp)
        
        # Generate the run functions
        run_tmp = CPPStandaloneCodeObject.templater.run(None, None, run_funcs=runfuncs,
                                                        code_objects=self.code_objects.values(),
                                                        additional_headers=run_includes,
                                                        )
        writer.write('run.*', run_tmp)

        # Copy the brianlibdirectory
        brianlib_dir = os.path.join(os.path.split(inspect.getsourcefile(CPPStandaloneCodeObject))[0],
                                    'brianlib')
        brianlib_files = copy_directory(brianlib_dir, os.path.join(project_dir, 'brianlib'))
        for file in brianlib_files:
            if file.lower().endswith('.cpp'):
                writer.source_files.append('brianlib/'+file)
            elif file.lower().endswith('.h'):
                writer.header_files.append('brianlib/'+file)

        # Copy the CSpikeQueue implementation
        spikequeue_h = os.path.join(project_dir, 'brianlib', 'spikequeue.h')
        shutil.copy2(os.path.join(os.path.split(inspect.getsourcefile(Synapses))[0], 'cspikequeue.cpp'),
                     spikequeue_h)
        #writer.header_files.append(spikequeue_h)
        
        writer.source_files.extend(additional_source_files)
        writer.header_files.extend(additional_header_files)

        # Generate the makefile
        if os.name=='nt':
            rm_cmd = 'del'
        else:
            rm_cmd = 'rm'
        makefile_tmp = CPPStandaloneCodeObject.templater.makefile(None, None,
                                                                  source_files=' '.join(writer.source_files),
                                                                  header_files=' '.join(writer.header_files),
                                                                  rm_cmd=rm_cmd)
        writer.write('makefile', makefile_tmp)

        # build the project
        if compile_project:
            with in_directory(project_dir):
                if debug:
                    x = os.system('make debug')
                elif native:
                    x = os.system('make native')
                else:
                    x = os.system('make')
                if x==0:
                    if run_project:
                        if not with_output:
                            stdout = open(os.devnull, 'w')
                        else:
                            stdout = None
                        if os.name=='nt':
                            x = subprocess.call(['main'] + run_args, stdout=stdout)
                        else:
                            x = subprocess.call(['./main'] + run_args, stdout=stdout)
                        if x:
                            raise RuntimeError("Project run failed")
                        self.has_been_run = True
                else:
                    raise RuntimeError("Project compilation failed")

    def network_run(self, net, duration, report=None, report_period=10*second,
                    namespace=None, level=0):

        # We have to use +2 for the level argument here, since this function is
        # called through the device_override mechanism
        net.before_run(namespace, level=level+2)
            
        self.clocks.update(net._clocks)

        # TODO: remove this horrible hack
        for clock in self.clocks:
            if clock.name=='clock':
                clock._name = '_clock'
            
        # Extract all the CodeObjects
        # Note that since we ran the Network object, these CodeObjects will be sorted into the right
        # running order, assuming that there is only one clock
        code_objects = []
        for obj in net.objects:
            for codeobj in obj._code_objects:
                code_objects.append((obj.clock, codeobj))

        # Code for a progress reporting function
        standard_code = '''
        void report_progress(const double elapsed, const double completed, const double duration)
        {
            if (completed == 0.0)
            {
                %STREAMNAME% << "Starting simulation for duration " << duration << " s";
            } else
            {
                %STREAMNAME% << completed*duration << " s (" << (int)(completed*100.) << "%) simulated in " << elapsed << " s";
                if (completed < 1.0)
                {
                    const int remaining = (int)((1-completed)/completed*elapsed+0.5);
                    %STREAMNAME% << ", estimated " << remaining << " s remaining.";
                }
            }

            %STREAMNAME% << std::endl << std::flush;
        }
        '''
        if report is None:
            self.report_func = ''
        elif report == 'text' or report == 'stdout':
            self.report_func = standard_code.replace('%STREAMNAME%', 'std::cout')
        elif report == 'stderr':
            self.report_func = standard_code.replace('%STREAMNAME%', 'std::cerr')
        elif isinstance(report, basestring):
            self.report_func = '''
            void report_progress(const double elapsed, const double completed, const double duration)
            {
            %REPORT%
            }
            '''.replace('%REPORT%', report)
        else:
            raise TypeError(('report argument has to be either "text", '
                             '"stdout", "stderr", or the code for a report '
                             'function'))

        if report is not None:
            report_call = 'report_progress'
        else:
            report_call = 'NULL'

        # Generate the updaters
        run_lines = ['{net.name}.clear();'.format(net=net)]
        for clock, codeobj in code_objects:
            run_lines.append('{net.name}.add(&{clock.name}, _run_{codeobj.name});'.format(clock=clock, net=net,
                                                                                               codeobj=codeobj))
        run_lines.append('{net.name}.run({duration}, {report_call}, {report_period});'.format(net=net,
                                                                                              duration=float(duration),
                                                                                              report_call=report_call,
                                                                                              report_period=float(report_period)))
        self.main_queue.append(('run_network', (net, run_lines)))

    def run_function(self, name, include_in_parent=True):
        '''
        Context manager to divert code into a function
        
        Code that happens within the scope of this context manager will go into the named function.
        
        Parameters
        ----------
        name : str
            The name of the function to divert code into.
        include_in_parent : bool
            Whether or not to include a call to the newly defined function in the parent context.
        '''
        return RunFunctionContext(name, include_in_parent)


class RunFunctionContext(object):
    def __init__(self, name, include_in_parent):
        self.name = name
        self.include_in_parent = include_in_parent
    def __enter__(self):
        cpp_standalone_device.main_queue.append(('start_run_func', (self.name, self.include_in_parent)))
    def __exit__(self, type, value, traceback):
        cpp_standalone_device.main_queue.append(('end_run_func', (self.name, self.include_in_parent)))


cpp_standalone_device = CPPStandaloneDevice()

all_devices['cpp_standalone'] = cpp_standalone_device
