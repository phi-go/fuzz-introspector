# Copyright 2024 Fuzz Introspector Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

from typing import Any, Optional

import os
import pathlib
import logging

from tree_sitter import Language, Parser, Node
import tree_sitter_cpp
import yaml

logger = logging.getLogger(name=__name__)
LOG_FMT = '%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s'


class SourceCodeFile():
    """Class for holding file-specific information."""

    def __init__(self,
                 source_file: str,
                 source_content: Optional[bytes] = None):
        logger.info('Processing %s' % source_file)

        self.source_file = source_file
        self.tree_sitter_lang = Language(tree_sitter_cpp.language())
        self.parser = Parser(self.tree_sitter_lang)

        self.root = None
        self.func_defs: list['FunctionDefinition'] = []

        if source_content:
            self.source_content = source_content
        else:
            with open(self.source_file, 'rb') as f:
                self.source_content = f.read()

        if self.source_content:
            # Initialization routines
            self.load_tree()
            self.process_tree(self.root)

    def load_tree(self):
        """Load the the source code into a treesitter tree, and set
        the root node."""
        if not self.root:
            self.root = self.parser.parse(self.source_content).root_node

    def process_tree(self, node: Node, namespace: str = ''):
        """Process the node from the parsed tree."""
        for child in node.children:
            if child.type == 'function_definition':
                self._process_function_node(child, namespace)
            elif child.type == 'namespace_definition':
                self._process_namespace_node(child, namespace)
            else:
                self.process_tree(child, namespace)

    def _process_namespace_node(self, node: Node, namespace: str = ''):
        """Recursive internal helper for processing namespace definition."""
        new_namespace = node.child_by_field_name('name')
        if new_namespace:
            # Nested namespace
            if new_namespace.type == 'nested_namespace_specifier':
                for child in new_namespace.children:
                    if not child.is_named:
                        continue
                    namespace += '::' + child.text.decode()
                    if namespace.startswith('::'):
                        namespace = namespace[2:]

            # General namespace
            elif new_namespace.type == 'namespace_identifier':
                namespace += '::' + new_namespace.text.decode()
                if namespace.startswith('::'):
                    namespace = namespace[2:]

        # Continue to process the tree of the namespace
        self.process_tree(node, namespace)

    def _process_function_node(self, node: Node, namespace: str = ''):
        """Internal helper for processing function node."""
        self.func_defs.append(
            FunctionDefinition(node, self.tree_sitter_lang, self, namespace))

    def get_function_node(
            self,
            target_function_name: str,
            exact: bool = False) -> Optional['FunctionDefinition']:
        """Gets the tree-sitter node corresponding to a function."""

        # Find the first instance of the function name
        for func in self.func_defs:
            if func.namespace_or_class:
                if func.namespace_or_class + '::' + func.name == target_function_name:
                    return func
            else:
                if func.name == target_function_name:
                    return func

        if exact:
            return None

        for func in self.func_defs:

            if func.name == target_function_name:
                return func

        for func in self.func_defs:
            if func.name == target_function_name.split('::')[-1]:
                return func
        return None

    def has_libfuzzer_harness(self) -> bool:
        """Returns whether the source code holds a libfuzzer harness"""
        for func in self.func_defs:
            if 'LLVMFuzzerTestOneInput' in func.name:
                return True

        return False


class FunctionDefinition():
    """Wrapper for a function definition"""

    def __init__(self, root: Node, tree_sitter_lang: Language,
                 source_code: 'SourceCodeFile', namespace: str):
        self.root = root
        self.tree_sitter_lang = tree_sitter_lang
        self.parent_source = source_code
        self.namespace_or_class = namespace

        # Store method line information
        self.start_line = self.root.start_point.row + 1
        self.end_line = self.root.end_point.row + 1

        # Other properties
        self.name = ''
        self.complexity = 0
        self.icount = 0
        self.arg_names: list[str] = []
        self.arg_types: list[str] = []
        self.return_type = ''
        self.sig = ''
        self.function_uses = 0
        self.function_depth = 0
        self.base_callsites: list[tuple[str, int]] = []
        self.detailed_callsites: list[dict[str, str]] = []

        # Extract information from tree-sitter node
        self._extract_information()

    def _extract_information(self):
        """Extract information from tree-sitter node."""
        # Extract function name and return type
        name_node = self.root.child_by_field_name('declarator')
        self.sig = name_node.text.decode()
        param_list_node = None
        for child in name_node.children:
            if 'identifier' in child.type:
                self.name = child.text.decode()

            elif child.type == 'function_declarator':
                for decl in child.children:
                    if 'identifier' in decl.type:
                        self.name = decl.text.decode()

                    elif decl.type == 'parameter_list':
                        param_list_node = decl

            elif child.type == 'parameter_list':
                param_list_node = child

        # Handles class or namespace in the function name
        if '::' in self.name:
            prefix, self.name = self.name.rsplit('::', 1)
            if self.namespace_or_class and prefix:
                self.namespace_or_class += f'::{prefix}'
            else:
                self.namespace_or_class = prefix

        # Handles return type
        type_node = self.root.child_by_field_name('type')
        if type_node:
            self.return_type = type_node.text.decode()
        else:
            self.return_type = 'void'

        # Handles parameters
        for param in param_list_node.children:
            if param.type == 'parameter_declaration':
                param_type = param.child_by_field_name('type')
                param_name = param.child_by_field_name('declarator')

                # Skip empty param name and type
                if not param_type or not param_name:
                    continue

                # Count pointer
                pointer_count = 0
                while param_name.type == 'pointer_declarator':
                    pointer_count += 1
                    param_name = param_name.child_by_field_name('declarator')

                # Count array
                array_count = 0
                while param_name.type == 'array_declarator':
                    array_count += 1
                    param_name = param_name.child_by_field_name('declarator')

                self.arg_types.append(
                    f'{param_type.text.decode()}{"*" * pointer_count}{"[]" * array_count}'
                )
                self.arg_names.append(param_name.text.decode())

        # Handles other fields
        self._process_complexity()
        self._process_icount()

    def _process_complexity(self):
        """Gets complexity measure based on counting branch nodes in a
        function."""

        branch_nodes = [
            'if_statement', 'switch_statement', 'do_statement',
            'while_statement', 'for_statement', 'for_range_loop',
            'try_statement', 'seh_try_statement', 'throw_statement',
            'goto_statement', 'co_return_statement', 'co_yield_statement',
            'break_statement', 'continue_statement', '&&', '||'
        ]

        def _traverse_node_complexity(node: Node):
            count = 0
            if node.type in branch_nodes:
                count += 1
            for item in node.children:
                count += _traverse_node_complexity(item)
            return count

        self.complexity += _traverse_node_complexity(self.root)

    def _process_icount(self):
        """Get a pseudo measurement of instruction count."""

        def _traverse_node_instr_count(node: Node) -> int:
            count = 0
            if 'statement' in node.type:
                count += 1
            for item in node.children:
                count += _traverse_node_instr_count(item)
            return count

        self.icount += _traverse_node_instr_count(self.root)

    def extract_callsites(self, functions: dict[str, 'FunctionDefinition']):
        """Gets the callsites of the function."""

        def _process_invoke(expr: Node) -> list[tuple[str, int, int]]:
            """Internal helper for processing the function invocation statement."""
            callsites = []
            target_name: str = ''

            func = expr.child_by_field_name('function')

            # Handle function call
            if func:
                # Simple function call
                # identifier indicates general function calls
                # qualified_identifier indicates namespace function calls
                # template_function indicates standard function calls
                if func.type in [
                        'identifier', 'qualified_identifier',
                        'template_function'
                ]:
                    target_name = func.text.decode()

                # Chained or method calls
                elif func.type == 'field_expression':
                    _, target_name = _process_field_expr_return_type(func)
                    callsites.append((target_name, func.byte_range[1],
                                      func.start_point.row + 1))

            if target_name:
                callsites.append((target_name, func.byte_range[1],
                                  func.start_point.row + 1))

            return callsites

        def _process_field_expr_return_type(
                field_expr: Node) -> tuple[Optional[str], str]:
            """Helper for determining the return type of a field expression
            in a chained call and its full qualified name."""
            type = None
            object_type = None

            arg = field_expr.child_by_field_name('argument')
            name = field_expr.child_by_field_name('field').text.decode()
            full_name = name

            # Chained field access
            if arg.type == 'field_expression':
                _, object_type = _process_field_expr_return_type(arg)

            # Internal call
            elif arg.type == 'this':
                object_type = self.namespace_or_class

            if object_type:
                if object_type == 'void':
                    full_name = name
                else:
                    full_name = f'{object_type}::{name}'

                node = get_function_node(full_name, functions)
                if node:
                    type = node.return_type

            return (type, full_name)

        def _process_callsites(stmt: Node) -> list[tuple[str, int, int]]:
            """Process and store the callsites of the function."""
            callsites = []

            # Call statement
            if stmt.type == 'call_expression':
                callsites.extend(_process_invoke(stmt))

            # Constructor call statement
            elif stmt.type == 'new_expression':
                ctr_type = stmt.child_by_field_name('type')
                if ctr_type:
                    callsites.append(
                        (ctr_type.text.decode(), stmt.byte_range[1],
                         stmt.start_point.row + 1))

            for child in stmt.children:
                callsites.extend(_process_callsites(child))

            return callsites

        if not self.base_callsites:
            callsites = []
            for child in self.root.children:
                callsites.extend(_process_callsites(child))

            callsites = sorted(set(callsites), key=lambda x: x[1])
            self.base_callsites = [(x[0], x[2]) for x in callsites]

        if not self.detailed_callsites:
            for dst, src_line in self.base_callsites:
                src_loc = self.parent_source.source_file + ':%d,1' % (src_line)
                self.detailed_callsites.append({'Src': src_loc, 'Dst': dst})


class Project():
    """Wrapper for doing analysis of a collection of source files."""

    def __init__(self, source_code_files: list[SourceCodeFile]):
        self.source_code_files = source_code_files

    def dump_module_logic(self,
                          report_name: str,
                          harness_name: Optional[str] = None):
        """Dumps the data for the module in full."""
        logger.info('Dumping project-wide logic.')
        report: dict[str, Any] = {'report': 'name'}
        report['sources'] = []

        self.all_functions = {}
        for source_code in self.source_code_files:
            # Log entry method if provided
            report['Fuzzing method'] = 'LLVMFuzzerTestOneInput'

            # Retrieve project information
            func_names = [func.name for func in source_code.func_defs]

            report['sources'].append({
                'source_file': source_code.source_file,
                'function_names': func_names,
            })

            # Obtain all functions of the project
            source_code_functions = {
                func.name: func
                for func in source_code.func_defs
            }

            self.all_functions.update(source_code_functions)

        # Process all project functions
        func_list = []
        for func in self.all_functions.values():
            func.extract_callsites(self.all_functions)

            func_dict: dict[str, Any] = {}
            func_dict['functionName'] = func.name
            func_dict['functionSourceFile'] = func.parent_source.source_file
            func_dict['functionLinenumber'] = func.start_line
            func_dict['functionLinenumberEnd'] = func.end_line
            func_dict['linkageType'] = ''
            func_dict['func_position'] = {
                'start': func.start_line,
                'end': func.end_line
            }
            func_dict['CyclomaticComplexity'] = func.complexity
            func_dict['EdgeCount'] = func_dict['CyclomaticComplexity']
            func_dict['ICount'] = func.icount
            func_dict['argNames'] = func.arg_names
            func_dict['argTypes'] = func.arg_types
            func_dict['argCount'] = len(func_dict['argTypes'])
            func_dict['returnType'] = func.return_type
            func_dict['BranchProfiles'] = []
            func_dict['Callsites'] = func.detailed_callsites
            func_dict['functionUses'] = self.calculate_function_uses(func.name)
            func_dict['functionDepth'] = self.calculate_function_depth(func)
            func_dict['constantsTouched'] = []
            func_dict['BBCount'] = 0
            func_dict['signature'] = func.sig
            callsites = func.base_callsites
            reached = set()
            for cs_dst, _ in callsites:
                reached.add(cs_dst)
            func_dict['functionsReached'] = list(reached)

            func_list.append(func_dict)

        if func_list:
            report['All functions'] = {}
            report['All functions']['Elements'] = func_list

        with open(report_name, 'w', encoding='utf-8') as f:
            f.write(yaml.dump(report))

    def get_source_codes_with_harnesses(self) -> list[SourceCodeFile]:
        """Gets the source codes that holds libfuzzer harnesses."""
        harnesses = []
        for source_code in self.source_code_files:
            if source_code.has_libfuzzer_harness():
                harnesses.append(source_code)
        return harnesses

    def extract_calltree(self,
                         source_file: str,
                         source_code: Optional[SourceCodeFile] = None,
                         function: Optional[str] = None,
                         visited_functions: Optional[set[str]] = None,
                         depth: int = 0,
                         line_number: int = -1) -> str:
        """Extracts calltree string of a calltree so that FI core can use it."""
        # Create calltree from a given function
        # Find the function in the source code
        if not visited_functions:
            visited_functions = set()

        if not function:
            return ''

        if not source_code:
            result = self.find_source_with_func_def(function)
            if result:
                source_code = result[0]

        func_node = None
        if function:
            func_node = get_function_node(function, self.all_functions)
            if func_node:
                func_name = func_node.name
                prefix = func_node.namespace_or_class
                if prefix:
                    func_name = f'{prefix}::{func_name}'
            else:
                func_name = function
        else:
            return ''

        line_to_print = '  ' * depth
        line_to_print += func_name
        line_to_print += ' '
        line_to_print += source_file

        line_to_print += ' '
        line_to_print += str(line_number)

        line_to_print += '\n'

        if function in visited_functions or not func_node or not source_code:
            return line_to_print

        visited_functions.add(function)
        for cs, line in func_node.base_callsites:
            line_to_print += self.extract_calltree(
                source_file=source_code.source_file,
                function=cs,
                visited_functions=visited_functions,
                depth=depth + 1,
                line_number=line)

        return line_to_print

    def find_source_with_func_def(
            self,
            name: str) -> Optional[tuple[SourceCodeFile, FunctionDefinition]]:
        """Finds the source code with a given function."""

        return_func = None
        source_codes_with_target = []
        for source_code in self.source_code_files:
            func = source_code.get_function_node(name, exact=True)
            if func:
                return_func = func
                source_codes_with_target.append(source_code)

        if len(source_codes_with_target) == 1 and return_func:
            # We hav have, in this case it's trivial.
            return (source_codes_with_target[0], return_func)

        return_func = None
        source_codes_with_target = []
        for source_code in self.source_code_files:
            func = source_code.get_function_node(name, exact=False)
            if func:
                return_func = func
                source_codes_with_target.append(source_code)

        if len(source_codes_with_target) == 1 and return_func:
            # We hav have, in this case it's trivial.
            return (source_codes_with_target[0], return_func)

        # TODO Handle multiple match (matching the namespace and class also

        return None

    def calculate_function_uses(self, target_name: str) -> int:
        """Calculate how many functions called the target function."""
        func_use_count = 0

        for source_file in self.source_code_files:
            for function in source_file.func_defs:
                found = False
                for callsite in function.base_callsites:
                    if callsite[0] == target_name:
                        found = True
                        break
                    elif callsite[0].endswith(target_name):
                        found = True
                        break
                if found:
                    func_use_count += 1

        return func_use_count

    def calculate_function_depth(self,
                                 target_function: FunctionDefinition) -> int:
        """Calculate function depth of the target function."""

        def _recursive_function_depth(function: FunctionDefinition) -> int:
            callsites = function.base_callsites
            if len(callsites) == 0:
                return 0

            depth = 0
            visited.append(function.name)
            for callsite in callsites:
                target = self.find_source_with_func_def(callsite[0])
                if target and target[1].name in visited:
                    depth = max(depth, 1)
                elif target:
                    depth = max(depth,
                                _recursive_function_depth(target[1]) + 1)
                else:
                    visited.append(callsite[0])

            return depth

        visited: list[str] = []
        func_depth = _recursive_function_depth(target_function)

        return func_depth


def capture_source_files_in_tree(directory_tree):
    """Captures source code files in a given directory."""
    language_files = []
    language_extensions = [
        '.cpp', '.cc', '.c++', '.cxx', '.h', '.hpp', '.hh', '.hxx', '.inl'
    ]
    exclude_directories = [
        'build', 'target', 'tests', 'node_modules', 'aflplusplus', 'honggfuzz',
        'inspector', 'libfuzzer', 'fuzztest'
    ]

    for dirpath, _, filenames in os.walk(directory_tree):
        # Skip some non project directories
        if any(exclude in dirpath for exclude in exclude_directories):
            continue

        for filename in filenames:
            if pathlib.Path(filename).suffix.lower() in language_extensions:
                language_files.append(os.path.join(dirpath, filename))

    return language_files


def load_treesitter_trees(source_files, is_log=True):
    """Creates treesitter trees for all files in a given list of source files."""
    results = []

    for code_file in source_files:
        if not os.path.isfile(code_file):
            continue

        source_cls = SourceCodeFile(code_file)
        results.append(source_cls)

        if is_log:
            if source_cls.has_libfuzzer_harness():
                logger.info('harness: %s', code_file)

    return results


def analyse_source_code(source_content: str) -> SourceCodeFile:
    """Returns a source abstraction based on a single source string."""
    source_code = SourceCodeFile(source_file='in-memory string',
                                 source_content=source_content.encode())
    return source_code


def get_function_node(
        target_name: str,
        function_map: dict[str, FunctionDefinition],
        one_layer_only: bool = False) -> Optional[FunctionDefinition]:
    """Helper to retrieve the RustFunction object of a function."""

    # Exact match
    if target_name in function_map:
        return function_map[target_name]

    # Match any key that ends with target_name, then
    # split the target_name by :: and check one by one
    if one_layer_only:
        name_split = target_name.split('::', 1)
    else:
        name_split = target_name.split('::')
    for count in range(len(name_split)):
        for func_name, func in function_map.items():
            if func_name.endswith('::'.join(name_split[count:])):
                return func

    return None
