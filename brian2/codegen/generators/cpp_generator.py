import itertools

import numpy

from brian2.utils.stringtools import (deindent, stripped_deindented_lines,
                                      word_substitute)
from brian2.utils.logger import get_logger
from brian2.parsing.rendering import CPPNodeRenderer
from brian2.core.functions import Function, DEFAULT_FUNCTIONS
from brian2.core.preferences import brian_prefs, BrianPreference
from brian2.core.variables import ArrayVariable

from .base import CodeGenerator

logger = get_logger(__name__)

__all__ = ['CPPCodeGenerator',
           'c_data_type',
           ]


def c_data_type(dtype):
    '''
    Gives the C language specifier for numpy data types. For example,
    ``numpy.int32`` maps to ``int32_t`` in C.
    '''
    # this handles the case where int is specified, it will be int32 or int64
    # depending on platform
    if dtype is int:
        dtype = numpy.array([1]).dtype.type
    if dtype is float:
        dtype = numpy.array([1.]).dtype.type

    if dtype == numpy.float32:
        dtype = 'float'
    elif dtype == numpy.float64:
        dtype = 'double'
    elif dtype == numpy.int8:
        dtype = 'int8_t'
    elif dtype == numpy.int16:
        dtype = 'int16_t'
    elif dtype == numpy.int32:
        dtype = 'int32_t'
    elif dtype == numpy.int64:
        dtype = 'int64_t'
    elif dtype == numpy.uint16:
        dtype = 'uint16_t'
    elif dtype == numpy.uint32:
        dtype = 'uint32_t'
    elif dtype == numpy.bool_ or dtype is bool:
        dtype = 'bool'
    else:
        raise ValueError("dtype " + str(dtype) + " not known.")
    return dtype


# Preferences
brian_prefs.register_preferences(
    'codegen.generators.cpp',
    'C++ codegen preferences',
    restrict_keyword = BrianPreference(
        default='__restrict__',
        docs='''
        The keyword used for the given compiler to declare pointers as restricted.
        
        This keyword is different on different compilers, the default is for gcc.
        ''',
        ),
    flush_denormals = BrianPreference(
        default=False,
        docs='''
        Adds code to flush denormals to zero.
        
        The code is gcc and architecture specific, so may not compile on all
        platforms. The code, for reference is::

            #define CSR_FLUSH_TO_ZERO         (1 << 15)
            unsigned csr = __builtin_ia32_stmxcsr();
            csr |= CSR_FLUSH_TO_ZERO;
            __builtin_ia32_ldmxcsr(csr);
            
        Found at `<http://stackoverflow.com/questions/2487653/avoiding-denormal-values-in-c>`_.
        ''',
        ),
    )


class CPPCodeGenerator(CodeGenerator):
    '''
    C++ language
    
    C++ code templates should provide Jinja2 macros with the following names:
    
    ``main``
        The main loop.
    ``support_code``
        The support code (function definitions, etc.), compiled in a separate
        file.
        
    For user-defined functions, there are two keys to provide:
    
    ``support_code``
        The function definition which will be added to the support code.
    ``hashdefine_code``
        The ``#define`` code added to the main loop.
        
    See `TimedArray` for an example of these keys.
    '''

    class_name = 'cpp'

    def __init__(self, *args, **kwds):
        super(CPPCodeGenerator, self).__init__(*args, **kwds)
        self.restrict = brian_prefs['codegen.generators.cpp.restrict_keyword'] + ' '
        self.flush_denormals = brian_prefs['codegen.generators.cpp.flush_denormals']
        self.c_data_type = c_data_type

    @staticmethod
    def get_array_name(var, access_data=True):
        # We have to do the import here to avoid circular import dependencies.
        from brian2.devices.device import get_device
        device = get_device()
        if access_data:
            return '_ptr' + device.get_array_name(var)
        else:
            return device.get_array_name(var, access_data=False)

    def translate_expression(self, expr):
        for varname, var in self.variables.iteritems():
            if isinstance(var, Function):
                impl_name = var.implementations[self.codeobj_class].name
                if impl_name is not None:
                    expr = word_substitute(expr, {varname: impl_name})
        return CPPNodeRenderer().render_expr(expr).strip()

    def translate_statement(self, statement):
        var, op, expr, comment = (statement.var, statement.op,
                                  statement.expr, statement.comment)
        if op == ':=':
            decl = self.c_data_type(statement.dtype) + ' '
            op = '='
            if statement.constant:
                decl = 'const ' + decl
        else:
            decl = ''
        code = decl + var + ' ' + op + ' ' + self.translate_expression(expr) + ';'
        if len(comment):
            code += ' // ' + comment
        return code
    
    def translate_to_read_arrays(self, statements):
        read, write, indices, conditional_write_vars = self.arrays_helper(statements)
        lines = []
        # index and read arrays (index arrays first)
        for varname in itertools.chain(indices, read):
            index_var = self.variable_indices[varname]
            var = self.variables[varname]
            if varname not in write:
                line = 'const '
            else:
                line = ''
            line = line + self.c_data_type(var.dtype) + ' ' + varname + ' = '
            line = line + self.get_array_name(var, self.variables) + '[' + index_var + '];'
            lines.append(line)
        return lines

    def translate_to_declarations(self, statements):
        read, write, indices, conditional_write_vars = self.arrays_helper(statements)
        lines = []
        # simply declare variables that will be written but not read
        for varname in write:
            if varname not in read:
                var = self.variables[varname]
                line = self.c_data_type(var.dtype) + ' ' + varname + ';'
                lines.append(line)
        return lines

    def translate_to_statements(self, statements):
        read, write, indices, conditional_write_vars = self.arrays_helper(statements)
        lines = []
        # the actual code
        for stmt in statements:
            line = self.translate_statement(stmt)
            if stmt.var in conditional_write_vars:
                subs = {}
                condvar = conditional_write_vars[stmt.var]
                lines.append('if(%s)' % condvar)
                lines.append('    '+line)
            else:
                lines.append(line)
        return lines

    def translate_to_write_arrays(self, statements):
        read, write, indices, conditional_write_vars = self.arrays_helper(statements)
        lines = []
        # write arrays
        for varname in write:
            index_var = self.variable_indices[varname]
            var = self.variables[varname]
            line = self.get_array_name(var, self.variables) + '[' + index_var + '] = ' + varname + ';'
            lines.append(line)
        return lines

    def translate_one_statement_sequence(self, statements):
        # This function is refactored into four functions which perform the
        # four necessary operations. It's done like this so that code
        # deriving from this class can overwrite specific parts.
        lines = []
        # index and read arrays (index arrays first)
        lines += self.translate_to_read_arrays(statements)
        # simply declare variables that will be written but not read
        lines += self.translate_to_declarations(statements)
        # the actual code
        lines += self.translate_to_statements(statements)
        # write arrays
        lines += self.translate_to_write_arrays(statements)
        code = '\n'.join(lines)                
        return stripped_deindented_lines(code)

    def denormals_to_zero_code(self):
        if self.flush_denormals:
            return '''
            #define CSR_FLUSH_TO_ZERO         (1 << 15)
            unsigned csr = __builtin_ia32_stmxcsr();
            csr |= CSR_FLUSH_TO_ZERO;
            __builtin_ia32_ldmxcsr(csr);
            '''
        else:
            return ''

    def determine_keywords(self):
        # set up the restricted pointers, these are used so that the compiler
        # knows there is no aliasing in the pointers, for optimisation
        lines = []
        # It is possible that several different variable names refer to the
        # same array. E.g. in gapjunction code, v_pre and v_post refer to the
        # same array if a group is connected to itself
        handled_pointers = set()
        template_kwds = {}
        # Again, do the import here to avoid a circular dependency.
        from brian2.devices.device import get_device
        device = get_device()
        for varname, var in self.variables.iteritems():
            if isinstance(var, ArrayVariable):
                # This is the "true" array name, not the restricted pointer.
                array_name = device.get_array_name(var)
                pointer_name = self.get_array_name(var)
                if pointer_name in handled_pointers:
                    continue
                if getattr(var, 'dimensions', 1) > 1:
                    continue  # multidimensional (dynamic) arrays have to be treated differently
                line = self.c_data_type(var.dtype) + ' * ' + self.restrict + pointer_name + ' = ' + array_name + ';'
                lines.append(line)
                handled_pointers.add(pointer_name)

        pointers = '\n'.join(lines)

        # set up the functions
        user_functions = []
        support_code = ''
        hash_defines = ''
        for varname, variable in self.variables.items():
            if isinstance(variable, Function):
                user_functions.append((varname, variable))
                funccode = variable.implementations[self.codeobj_class].get_code(self.owner)
                if funccode is not None:
                    support_code += '\n' + deindent(funccode.get('support_code', ''))
                    hash_defines += '\n' + deindent(funccode.get('hashdefine_code', ''))
                # add the Python function with a leading '_python', if it
                # exists. This allows the function to make use of the Python
                # function via weave if necessary (e.g. in the case of randn)
                if not variable.pyfunc is None:
                    pyfunc_name = '_python_' + varname
                    if pyfunc_name in self.variables:
                        logger.warn(('Namespace already contains function %s, '
                                     'not replacing it') % pyfunc_name)
                    else:
                        self.variables[pyfunc_name] = variable.pyfunc

        # delete the user-defined functions from the namespace and add the
        # function namespaces (if any)
        for funcname, func in user_functions:
            del self.variables[funcname]
            func_namespace = func.implementations[self.codeobj_class].get_namespace(self.owner)
            if func_namespace is not None:
                self.variables.update(func_namespace)

        keywords = {'pointers_lines': stripped_deindented_lines(pointers),
                    'support_code_lines': stripped_deindented_lines(support_code),
                    'hashdefine_lines': stripped_deindented_lines(hash_defines),
                    'denormals_code_lines': stripped_deindented_lines(self.denormals_to_zero_code()),
                    }
        keywords.update(template_kwds)
        return keywords

    def translate_statement_sequence(self, statements):
        scalar_code = {}
        vector_code = {}
        for name, block in statements.iteritems():
            scalar_statements = [stmt for stmt in block if stmt.scalar]
            vector_statements = [stmt for stmt in block if not stmt.scalar]
            scalar_code[name] = self.translate_one_statement_sequence(scalar_statements)
            vector_code[name] = self.translate_one_statement_sequence(vector_statements)

        kwds = self.determine_keywords()

        return scalar_code, vector_code, kwds


################################################################################
# Implement functions
################################################################################

# Functions that exist under the same name in C++
for func in ['sin', 'cos', 'tan', 'sinh', 'cosh', 'tanh', 'exp', 'log',
             'log10', 'sqrt', 'ceil', 'floor']:
    DEFAULT_FUNCTIONS[func].implementations.add_implementation(CPPCodeGenerator,
                                                               code=None)

# Functions that need a name translation
for func, func_cpp in [('arcsin', 'asin'), ('arccos', 'acos'), ('arctan', 'atan'),
                       ('abs', 'fabs'), ('mod', 'fmod')]:
    DEFAULT_FUNCTIONS[func].implementations.add_implementation(CPPCodeGenerator,
                                                               code=None,
                                                               name=func_cpp)

# Functions that need to be implemented specifically
randn_code = {'support_code': '''

    inline double _ranf()
    {
        return (double)rand()/RAND_MAX;
    }

    double _randn(const int vectorisation_idx)
    {
         double x1, x2, w;
         static double y1, y2;
         static bool need_values = true;
         if (need_values)
         {
             do {
                     x1 = 2.0 * _ranf() - 1.0;
                     x2 = 2.0 * _ranf() - 1.0;
                     w = x1 * x1 + x2 * x2;
             } while ( w >= 1.0 );

             w = sqrt( (-2.0 * log( w ) ) / w );
             y1 = x1 * w;
             y2 = x2 * w;

             need_values = false;
             return y1;
         } else
         {
            need_values = true;
            return y2;
         }
    }
        '''}
DEFAULT_FUNCTIONS['randn'].implementations.add_implementation(CPPCodeGenerator,
                                                              code=randn_code,
                                                              name='_randn')

rand_code = {'support_code': '''
        double _rand(int vectorisation_idx)
        {
	        return (double)rand()/RAND_MAX;
        }
        '''}
DEFAULT_FUNCTIONS['rand'].implementations.add_implementation(CPPCodeGenerator,
                                                             code=rand_code,
                                                             name='_rand')

clip_code = {'support_code': '''
        double _clip(const float value, const float a_min, const float a_max)
        {
	        if (value < a_min)
	            return a_min;
	        if (value > a_max)
	            return a_max;
	        return value;
	    }
        '''}
DEFAULT_FUNCTIONS['clip'].implementations.add_implementation(CPPCodeGenerator,
                                                             code=clip_code,
                                                             name='_clip')

int_code = {'support_code':
        '''
        int int_(const bool value)
        {
	        return value ? 1 : 0;
        }
        '''}
DEFAULT_FUNCTIONS['int'].implementations.add_implementation(CPPCodeGenerator,
                                                            code=int_code,
                                                            name='int_')
