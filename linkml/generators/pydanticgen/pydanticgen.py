import inspect
import logging
import os
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Literal, Optional, Set, Type, TypeVar, Union, overload

import click
from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from linkml_runtime.linkml_model.meta import (
    ClassDefinition,
    SchemaDefinition,
    SlotDefinition,
    TypeDefinition,
)
from linkml_runtime.utils.compile_python import compile_python
from linkml_runtime.utils.formatutils import camelcase, remove_empty_items, underscore
from linkml_runtime.utils.schemaview import SchemaView
from pydantic.version import VERSION as PYDANTIC_VERSION

from linkml._version import __version__
from linkml.generators.common.lifecycle import LifecycleMixin
from linkml.generators.common.type_designators import get_accepted_type_designator_values, get_type_designator_value
from linkml.generators.oocodegen import OOCodeGenerator
from linkml.generators.pydanticgen import includes
from linkml.generators.pydanticgen.array import ArrayRangeGenerator, ArrayRepresentation
from linkml.generators.pydanticgen.build import ClassResult, SlotResult
from linkml.generators.pydanticgen.template import (
    Import,
    Imports,
    ObjectImport,
    PydanticAttribute,
    PydanticBaseModel,
    PydanticClass,
    PydanticModule,
    PydanticTemplateModel,
)
from linkml.utils import deprecation_warning
from linkml.utils.generator import shared_arguments
from linkml.utils.ifabsent_functions import ifabsent_value_declaration

if int(PYDANTIC_VERSION[0]) == 1:
    deprecation_warning("pydantic-v1")


def _get_pyrange(t: TypeDefinition, sv: SchemaView) -> str:
    pyrange = t.repr if t is not None else None
    if pyrange is None:
        pyrange = t.base
    if t.base == "XSDDateTime":
        pyrange = "datetime "
    if t.base == "XSDDate":
        pyrange = "date"
    if pyrange is None and t.typeof is not None:
        pyrange = _get_pyrange(sv.get_type(t.typeof), sv)
    if pyrange is None:
        raise Exception(f"No python type for range: {t.name} // {t}")
    return pyrange


DEFAULT_IMPORTS = (
    Imports()
    + Import(module="__future__", objects=[ObjectImport(name="annotations")])
    + Import(module="datetime", objects=[ObjectImport(name="datetime"), ObjectImport(name="date")])
    + Import(module="decimal", objects=[ObjectImport(name="Decimal")])
    + Import(module="enum", objects=[ObjectImport(name="Enum")])
    + Import(module="re")
    + Import(module="sys")
    + Import(
        module="typing",
        objects=[
            ObjectImport(name="Any"),
            ObjectImport(name="ClassVar"),
            ObjectImport(name="List"),
            ObjectImport(name="Literal"),
            ObjectImport(name="Dict"),
            ObjectImport(name="Optional"),
            ObjectImport(name="Union"),
        ],
    )
    + Import(
        module="pydantic",
        objects=[
            ObjectImport(name="BaseModel"),
            ObjectImport(name="ConfigDict"),
            ObjectImport(name="Field"),
            ObjectImport(name="RootModel"),
            ObjectImport(name="field_validator"),
        ],
    )
)

DEFAULT_INJECTS = [includes.LinkMLMeta]


class MetadataMode(str, Enum):
    FULL = "full"
    """
    all metadata from the source schema will be included, even if it is represented by the template classes, 
    and even if it is represented by some child class (eg. "classes" will be included with schema metadata
    """
    EXCEPT_CHILDREN = "except_children"
    """
    all metadata from the source schema will be included, even if it is represented by the template classes,
    except if it is represented by some child template class (eg. "classes" will be excluded from schema metadata)
    """
    AUTO = "auto"
    """
    Only the metadata that isn't represented by the template classes or excluded with ``meta_exclude`` will be included 
    """
    NONE = None
    """
    No metadata will be included.
    """


DefinitionType = TypeVar("DefinitionType", bound=Union[SchemaDefinition, ClassDefinition, SlotDefinition])
TemplateType = TypeVar("TemplateType", bound=Union[PydanticModule, PydanticClass, PydanticAttribute])


@dataclass
class PydanticGenerator(OOCodeGenerator, LifecycleMixin):
    """
    Generates Pydantic-compliant classes from a schema

    This is an alternative to the dataclasses-based Pythongen

    Lifecycle methods (see :class:`.LifecycleMixin` ) supported:

    * :meth:`~.LifecycleMixin.before_generate_enums`

    Slot generation is nested within class generation, since the pydantic generator currently doesn't
    create an independent representation of slots aside from their materialization as class fields.
    Accordingly, the ``before_`` and ``after_generate_slots`` are called before and after each class's
    slot generation, rather than all slot generation.

    * :meth:`~.LifecycleMixin.before_generated_classes`
    * :meth:`~.LifecycleMixin.before_generate_class`
    * :meth:`~.LifecycleMixin.after_generate_class`
    * :meth:`~.LifecycleMixin.after_generate_classes`

    * :meth:`~.LifecycleMixin.before_generate_slots`
    * :meth:`~.LifecycleMixin.before_generate_slot`
    * :meth:`~.LifecycleMixin.after_generate_slot`
    * :meth:`~.LifecycleMixin.after_generate_slots`

    * :meth:`~.LifecycleMixin.before_render_template`
    * :meth:`~.LifecycleMixin.after_render_template`

    """

    # ClassVar overrides
    generatorname = os.path.basename(__file__)
    generatorversion = "0.0.2"
    valid_formats = ["pydantic"]
    file_extension = "py"

    # ObjectVars
    array_representations: List[ArrayRepresentation] = field(default_factory=lambda: [ArrayRepresentation.LIST])
    black: bool = False
    """
    If black is present in the environment, format the serialized code with it
    """

    template_dir: Optional[Union[str, Path]] = None
    """
    Override templates for each PydanticTemplateModel.
    
    Directory with templates that override the default :attr:`.PydanticTemplateModel.template`
    for each class. If a matching template is not found in the override directory,
    the default templates will be used.
    """
    extra_fields: Literal["allow", "forbid", "ignore"] = "forbid"
    gen_mixin_inheritance: bool = True
    injected_classes: Optional[List[Union[Type, str]]] = None
    """
    A list/tuple of classes to inject into the generated module.
    
    Accepts either live classes or strings. Live classes will have their source code
    extracted with inspect.get - so they need to be standard python classes declared in a
    source file (ie. the module they are contained in needs a ``__file__`` attr, 
    see: :func:`inspect.getsource` )
    """
    injected_fields: Optional[List[str]] = None
    """
    A list/tuple of field strings to inject into the base class.
    
    Examples:
    
    .. code-block:: python

        injected_fields = (
            'object_id: Optional[str] = Field(None, description="Unique UUID for each object")',
        )
    
    """
    imports: Optional[List[Import]] = None
    """
    Additional imports to inject into generated module. 
    
    Examples:
        
    .. code-block:: python
    
        from linkml.generators.pydanticgen.template import (
            ConditionalImport,
            ObjectImport,
            Import,
            Imports
        )
        
        imports = (Imports() + 
            Import(module='sys') + 
            Import(module='numpy', alias='np') + 
            Import(module='pathlib', objects=[
                ObjectImport(name="Path"),
                ObjectImport(name="PurePath", alias="RenamedPurePath")
            ]) + 
            ConditionalImport(
                module="typing",
                objects=[ObjectImport(name="Literal")],
                condition="sys.version_info >= (3, 8)",
                alternative=Import(
                    module="typing_extensions", 
                    objects=[ObjectImport(name="Literal")]
                ),
            ).imports
        )
        
    becomes:
    
    .. code-block:: python
    
        import sys
        import numpy as np
        from pathlib import (
            Path,
            PurePath as RenamedPurePath
        )
        if sys.version_info >= (3, 8):
            from typing import Literal
        else:
            from typing_extensions import Literal
        
    """
    metadata_mode: Union[MetadataMode, str, None] = MetadataMode.AUTO
    """
    How to include schema metadata in generated pydantic models.
    
    See :class:`.MetadataMode` for mode documentation
    """

    # ObjectVars (identical to pythongen)
    gen_classvars: bool = True
    gen_slots: bool = True
    genmeta: bool = False
    emit_metadata: bool = True

    # Private attributes
    _predefined_slot_values: Optional[Dict[str, Dict[str, str]]] = None
    _class_bases: Optional[Dict[str, List[str]]] = None

    def __post_init__(self):
        super().__post_init__()

    def compile_module(self, **kwargs) -> ModuleType:
        """
        Compiles generated python code to a module
        :return:
        """
        pycode = self.serialize(**kwargs)
        try:
            return compile_python(pycode)
        except NameError as e:
            logging.error(f"Code:\n{pycode}")
            logging.error(f"Error compiling generated python code: {e}")
            raise e

    @staticmethod
    def sort_classes(clist: List[ClassDefinition]) -> List[ClassDefinition]:
        """
        sort classes such that if C is a child of P then C appears after P in the list

        Overridden method include mixin classes

        TODO: This should move to SchemaView
        """
        clist = list(clist)
        slist = []  # sorted
        while len(clist) > 0:
            can_add = False
            for i in range(len(clist)):
                candidate = clist[i]
                can_add = False
                if candidate.is_a:
                    candidates = [candidate.is_a] + candidate.mixins
                else:
                    candidates = candidate.mixins
                if not candidates:
                    can_add = True
                else:
                    if set(candidates) <= set([p.name for p in slist]):
                        can_add = True
                if can_add:
                    slist = slist + [candidate]
                    del clist[i]
                    break
            if not can_add:
                raise ValueError(f"could not find suitable element in {clist} that does not ref {slist}")
        return slist

    def generate_class(self, cls: ClassDefinition) -> ClassResult:
        pyclass = PydanticClass(
            name=camelcase(cls.name),
            bases=self.class_bases.get(cls.name, PydanticBaseModel.default_name),
            description=cls.description.replace('"', '\\"') if cls.description is not None else None,
        )

        result = ClassResult(cls=pyclass)

        # Gather slots
        slots = [self.schemaview.induced_slot(sn, cls.name) for sn in self.schemaview.class_slots(cls.name)]
        slots = self.before_generate_slots(slots, self.schemaview)

        slot_results = []
        for slot in slots:
            slot = self.before_generate_slot(slot, self.schemaview)
            slot = self.generate_slot(slot, cls)
            slot = self.after_generate_slot(slot, self.schemaview)
            slot_results.append(slot)
            result = result.merge(slot)

        slot_results = self.after_generate_slots(slot_results, self.schemaview)
        attributes = {slot.attribute.name: slot.attribute for slot in slot_results}

        result.cls.attributes = attributes
        result.cls = self.include_metadata(result.cls, cls)

        return result

    def generate_slot(self, slot: SlotDefinition, cls: ClassDefinition) -> SlotResult:
        slot_args = {
            k: slot._as_dict.get(k, None)
            for k in PydanticAttribute.model_fields.keys()
            if slot._as_dict.get(k, None) is not None
        }
        slot_args["name"] = underscore(slot.name)
        slot_args["description"] = slot.description.replace('"', '\\"') if slot.description is not None else None
        predef = self.predefined_slot_values.get(cls.name, {}).get(slot.name, None)
        if predef is not None:
            slot_args["predefined"] = str(predef)

        pyslot = PydanticAttribute(**slot_args)
        pyslot = self.include_metadata(pyslot, slot)

        slot_ranges = []
        # Confirm that the original slot range (ignoring the default that comes in from
        # induced_slot) isn't in addition to setting any_of
        any_of_ranges = [a.range if a.range else slot.range for a in slot.any_of]
        if any_of_ranges:
            # list comprehension here is pulling ranges from within AnonymousSlotExpression
            slot_ranges.extend(any_of_ranges)
        else:
            slot_ranges.append(slot.range)

        pyranges = [self.generate_python_range(slot_range, slot, cls) for slot_range in slot_ranges]

        pyranges = list(set(pyranges))  # remove duplicates
        pyranges.sort()

        if len(pyranges) == 1:
            pyrange = pyranges[0]
        elif len(pyranges) > 1:
            pyrange = f"Union[{', '.join(pyranges)}]"
        else:
            raise Exception(f"Could not generate python range for {cls.name}.{slot.name}")

        pyslot.range = pyrange
        result = SlotResult(attribute=pyslot)

        if slot.array is not None:
            results = self.get_array_representations_range(slot, result.attribute.range)
            if len(results) == 1:
                result.attribute.range = results[0].range
            else:
                result.attribute.range = f"Union[{', '.join([res.range for res in results])}]"
            for res in results:
                result = result.merge(res)

        elif slot.multivalued:
            if slot.inlined or slot.inlined_as_list:
                collection_key = self.generate_collection_key(slot_ranges, slot, cls)
            else:
                collection_key = None
            if slot.inlined is False or collection_key is None or slot.inlined_as_list is True:
                result.attribute.range = f"List[{result.attribute.range}]"
            else:
                simple_dict_value = None
                if len(slot_ranges) == 1:
                    simple_dict_value = self._inline_as_simple_dict_with_value(slot)
                if simple_dict_value:
                    # simple_dict_value might be the range of the identifier of a class when range is a class,
                    # so we specify either that identifier or the range itself
                    if simple_dict_value != result.attribute.range:
                        simple_dict_value = f"Union[{simple_dict_value}, {result.attribute.range}]"
                    result.attribute.range = f"Dict[str, {simple_dict_value}]"
                else:
                    result.attribute.range = f"Dict[{collection_key}, {result.attribute.range}]"
        if not (slot.required or slot.identifier or slot.key) and not slot.designates_type:
            result.attribute.range = f"Optional[{result.attribute.range}]"
        return result

    @property
    def predefined_slot_values(self) -> Dict[str, Dict[str, str]]:
        """
        :return: Dictionary of dictionaries with predefined slot values for each class
        """
        if self._predefined_slot_values is None:
            sv = self.schemaview
            slot_values = defaultdict(dict)
            for class_def in sv.all_classes().values():
                for slot_name in sv.class_slots(class_def.name):
                    slot = sv.induced_slot(slot_name, class_def.name)
                    if slot.designates_type:
                        target_value = get_type_designator_value(sv, slot, class_def)
                        slot_values[camelcase(class_def.name)][slot.name] = f'"{target_value}"'
                        if slot.multivalued:
                            slot_values[camelcase(class_def.name)][slot.name] = (
                                "[" + slot_values[camelcase(class_def.name)][slot.name] + "]"
                            )
                        slot_values[camelcase(class_def.name)][slot.name] = slot_values[camelcase(class_def.name)][
                            slot.name
                        ]
                    elif slot.ifabsent is not None:
                        value = ifabsent_value_declaration(slot.ifabsent, sv, class_def, slot)
                        slot_values[camelcase(class_def.name)][slot.name] = value
                    # Multivalued slots that are either not inlined (just an identifier) or are
                    # inlined as lists should get default_factory list, if they're inlined but
                    # not as a list, that means a dictionary
                    elif slot.multivalued:
                        has_identifier_slot = self.range_class_has_identifier_slot(slot)

                        if slot.inlined and not slot.inlined_as_list and has_identifier_slot:
                            slot_values[camelcase(class_def.name)][slot.name] = "default_factory=dict"
                        else:
                            slot_values[camelcase(class_def.name)][slot.name] = "default_factory=list"
            self._predefined_slot_values = slot_values

        return self._predefined_slot_values

    @property
    def class_bases(self) -> Dict[str, List[str]]:
        """
        Generate the inheritance list for each class from is_a plus mixins
        :return:
        """
        if self._class_bases is None:
            sv = self.schemaview
            parents = {}
            for class_def in sv.all_classes().values():
                class_parents = []
                if class_def.is_a:
                    class_parents.append(camelcase(class_def.is_a))
                if self.gen_mixin_inheritance and class_def.mixins:
                    class_parents.extend([camelcase(mixin) for mixin in class_def.mixins])
                if len(class_parents) > 0:
                    # Use the sorted list of classes to order the parent classes, but reversed to match MRO needs
                    class_parents.sort(key=lambda x: self.sorted_class_names.index(x))
                    class_parents.reverse()
                    parents[camelcase(class_def.name)] = class_parents
            self._class_bases = parents
        return self._class_bases

    def range_class_has_identifier_slot(self, slot):
        """
        Check if the range class of a slot has an identifier slot, via both slot.any_of and slot.range
        Should return False if the range is not a class, and also if the range is a class but has no
        identifier slot

        :param slot: SlotDefinition
        :return: bool
        """
        sv = self.schemaview
        has_identifier_slot = False
        if slot.any_of:
            for slot_range in slot.any_of:
                any_of_range = slot_range.range
                if any_of_range in sv.all_classes() and sv.get_identifier_slot(any_of_range, use_key=True) is not None:
                    has_identifier_slot = True
        if slot.range in sv.all_classes() and sv.get_identifier_slot(slot.range, use_key=True) is not None:
            has_identifier_slot = True
        return has_identifier_slot

    def get_mixin_identifier_range(self, mixin) -> str:
        sv = self.schemaview
        id_ranges = list(
            {
                _get_pyrange(sv.get_type(sv.get_identifier_slot(c).range), sv)
                for c in sv.class_descendants(mixin.name, mixins=True)
                if sv.get_identifier_slot(c) is not None
            }
        )
        if len(id_ranges) == 0:
            return None
        elif len(id_ranges) == 1:
            return id_ranges[0]
        else:
            return f"Union[{'.'.join(id_ranges)}]"

    def get_class_slot_range(self, slot_range: str, inlined: bool, inlined_as_list: bool) -> str:
        sv = self.schemaview
        range_cls = sv.get_class(slot_range)

        # Hardcoded handling for Any
        if range_cls.class_uri == "linkml:Any":
            return "Any"

        # Inline the class itself only if the class is defined as inline, or if the class has no
        # identifier slot and also isn't a mixin.
        if (
            inlined
            or inlined_as_list
            or (sv.get_identifier_slot(range_cls.name, use_key=True) is None and not sv.is_mixin(range_cls.name))
        ):
            if (
                len([x for x in sv.class_induced_slots(slot_range) if x.designates_type]) > 0
                and len(sv.class_descendants(slot_range)) > 1
            ):
                return "Union[" + ",".join([camelcase(c) for c in sv.class_descendants(slot_range)]) + "]"
            else:
                return f"{camelcase(slot_range)}"

        # For the more difficult cases, set string as the default and attempt to improve it
        range_cls_identifier_slot_range = "str"

        # For mixins, try to use the identifier slot of descendant classes
        if self.gen_mixin_inheritance and sv.is_mixin(range_cls.name) and sv.get_identifier_slot(range_cls.name):
            range_cls_identifier_slot_range = self.get_mixin_identifier_range(range_cls)

        # If the class itself has an identifier slot, it can be allowed to overwrite a value from mixin above
        if (
            sv.get_identifier_slot(range_cls.name) is not None
            and sv.get_identifier_slot(range_cls.name).range is not None
        ):
            range_cls_identifier_slot_range = _get_pyrange(
                sv.get_type(sv.get_identifier_slot(range_cls.name).range), sv
            )

        return range_cls_identifier_slot_range

    def generate_python_range(self, slot_range, slot_def: SlotDefinition, class_def: ClassDefinition) -> str:
        """
        Generate the python range for a slot range value
        """
        sv = self.schemaview

        if slot_def.designates_type:
            pyrange = (
                "Literal["
                + ",".join(['"' + x + '"' for x in get_accepted_type_designator_values(sv, slot_def, class_def)])
                + "]"
            )
        elif slot_range in sv.all_classes():
            pyrange = self.get_class_slot_range(
                slot_range,
                inlined=slot_def.inlined,
                inlined_as_list=slot_def.inlined_as_list,
            )
        elif slot_range in sv.all_enums():
            pyrange = f"{camelcase(slot_range)}"
        elif slot_range in sv.all_types():
            t = sv.get_type(slot_range)
            pyrange = _get_pyrange(t, sv)
        elif slot_range is None:
            pyrange = "str"
        else:
            # TODO: default ranges in schemagen
            # pyrange = 'str'
            # logging.error(f'range: {s.range} is unknown')
            raise Exception(f"range: {slot_range}")
        return pyrange

    def generate_collection_key(
        self,
        slot_ranges: List[str],
        slot_def: SlotDefinition,
        class_def: ClassDefinition,
    ) -> Optional[str]:
        """
        Find the python range value (str, int, etc) for the identifier slot
        of a class used as a slot range.

        If a pyrange value matches a class name, the range of the identifier slot
        will be returned. If more than one match is found and they don't match,
        an exception will be raised.

        :param slot_ranges: list of python range values
        """

        collection_keys: Set[str] = set()

        if slot_ranges is None:
            return None

        for slot_range in slot_ranges:
            if slot_range is None or slot_range not in self.schemaview.all_classes():
                continue  # ignore non-class ranges

            identifier_slot = self.schemaview.get_identifier_slot(slot_range, use_key=True)
            if identifier_slot is not None:
                collection_keys.add(self.generate_python_range(identifier_slot.range, slot_def, class_def))
        if len(collection_keys) > 1:
            raise Exception(f"Slot with any_of range has multiple identifier slot range types: {collection_keys}")
        if len(collection_keys) == 1:
            return list(collection_keys)[0]
        return None

    def _clean_injected_classes(self, injected_classes: List[Union[str, Type]]) -> Optional[List[str]]:
        """Get source, deduplicate, and dedent injected classes"""
        if len(injected_classes) == 0:
            return None

        injected_classes = list(
            dict.fromkeys([c if isinstance(c, str) else inspect.getsource(c) for c in injected_classes])
        )
        injected_classes = [textwrap.dedent(c) for c in injected_classes]
        return injected_classes

    def _inline_as_simple_dict_with_value(self, slot_def: SlotDefinition) -> Optional[str]:
        """
        Determine if a slot should be inlined as a simple dict with a value.

        For example, if we have a class such as Prefix, with two slots prefix and expansion,
        then an inlined list of prefixes can be serialized as:

        .. code-block:: yaml

            prefixes:
                prefix1: expansion1
                prefix2: expansion2
                ...

        Provided that the prefix slot is the identifier slot for the Prefix class.

        TODO: move this to SchemaView

        :param slot_def: SlotDefinition
        :param sv: SchemaView
        :return: str
        """
        if slot_def.inlined and not slot_def.inlined_as_list:
            if slot_def.range in self.schemaview.all_classes():
                id_slot = self.schemaview.get_identifier_slot(slot_def.range, use_key=True)
                if id_slot is not None:
                    range_cls_slots = self.schemaview.class_induced_slots(slot_def.range)
                    if len(range_cls_slots) == 2:
                        non_id_slots = [slot for slot in range_cls_slots if slot.name != id_slot.name]
                        if len(non_id_slots) == 1:
                            value_slot = non_id_slots[0]
                            value_slot_range_type = self.schemaview.get_type(value_slot.range)
                            if value_slot_range_type is not None:
                                return _get_pyrange(value_slot_range_type, self.schemaview)
        return None

    def _template_environment(self) -> Environment:
        env = PydanticTemplateModel.environment()
        if self.template_dir is not None:
            loader = ChoiceLoader([FileSystemLoader(self.template_dir), env.loader])
            env.loader = loader
        return env

    def get_array_representations_range(self, slot: SlotDefinition, range: str) -> List[SlotResult]:
        """
        Generate the python range for array representations
        """
        array_reps = []
        for repr in self.array_representations:
            generator = ArrayRangeGenerator.get_generator(repr)
            result = generator(slot.array, range).make()
            array_reps.append(result)

        if len(array_reps) == 0:
            raise ValueError("No array representation generated, but one was requested!")

        return array_reps

    @overload
    def include_metadata(self, model: PydanticModule, source: SchemaDefinition) -> PydanticModule: ...

    @overload
    def include_metadata(self, model: PydanticClass, source: ClassDefinition) -> PydanticClass: ...

    @overload
    def include_metadata(self, model: PydanticAttribute, source: SlotDefinition) -> PydanticAttribute: ...

    def include_metadata(self, model: TemplateType, source: DefinitionType) -> TemplateType:
        """
        Include metadata from the source schema that is otherwise not represented in the pydantic template models.

        Metadata inclusion mode is dependent on :attr:`.metadata_mode` - see:

        - :class:`.MetadataMode`
        - :meth:`.PydanticTemplateModel.exclude_from_meta`

        """
        if self.metadata_mode is None or self.metadata_mode == MetadataMode.NONE:
            return model
        elif self.metadata_mode in (MetadataMode.AUTO, MetadataMode.AUTO.value):
            meta = {k: v for k, v in remove_empty_items(source).items() if k not in model.exclude_from_meta()}
        elif self.metadata_mode in (MetadataMode.EXCEPT_CHILDREN, MetadataMode.EXCEPT_CHILDREN.value):
            meta = {}
            for k, v in remove_empty_items(source).items():
                if k in ("slots", "classes") and isinstance(model, PydanticModule):
                    # FIXME: Special-case removal of slots until we generate class-level slots
                    continue

                if not hasattr(model, k):
                    meta[k] = v
                    continue

                model_attr = getattr(model, k)
                if isinstance(model_attr, list) and not any(
                    [isinstance(item, PydanticTemplateModel) for item in model_attr]
                ):
                    meta[k] = v
                elif isinstance(model_attr, dict) and not any(
                    [isinstance(item, PydanticTemplateModel) for item in model_attr.values()]
                ):
                    meta[k] = v
                elif not isinstance(model_attr, (list, dict, PydanticTemplateModel)):
                    meta[k] = v

        elif self.metadata_mode in (MetadataMode.FULL, MetadataMode.FULL.value):
            meta = remove_empty_items(source)
        else:
            raise ValueError(
                f"Unknown metadata mode '{self.metadata_mode}', needs to be one of "
                f"{[mode for mode in MetadataMode]}"
            )

        model.meta = meta
        return model

    def render(self) -> PydanticModule:
        sv: SchemaView
        sv = self.schemaview

        # imports
        imports = DEFAULT_IMPORTS
        if self.imports is not None:
            for i in self.imports:
                imports += i

        # injected classes
        injected_classes = DEFAULT_INJECTS.copy()
        if self.injected_classes is not None:
            injected_classes += self.injected_classes.copy()

        # enums
        enums = self.before_generate_enums(list(sv.all_enums().values()), sv)
        enums = self.generate_enums({e.name: e for e in enums})

        base_model = PydanticBaseModel(extra_fields=self.extra_fields, fields=self.injected_fields)

        # schema classes
        class_results = []
        source_classes = self.sort_classes(list(sv.all_classes().values()))
        # Don't want to generate classes when class_uri is linkml:Any, will
        # just swap in typing.Any instead down below
        source_classes = [c for c in source_classes if c.class_uri != "linkml:Any"]
        source_classes = self.before_generate_classes(source_classes, sv)
        self.sorted_class_names = [camelcase(c.name) for c in source_classes]
        for cls in source_classes:
            cls = self.before_generate_class(cls, sv)
            result = self.generate_class(cls)
            result = self.after_generate_class(result, sv)
            class_results.append(result)
            # classes[result.cls.name] = result.cls
            if result.imports is not None:
                imports += result.imports
            if result.injected_classes is not None:
                injected_classes.extend(result.injected_classes)

        class_results = self.after_generate_classes(class_results, sv)

        classes = {r.cls.name: r.cls for r in class_results}

        injected_classes = self._clean_injected_classes(injected_classes)

        module = PydanticModule(
            metamodel_version=self.schema.metamodel_version,
            version=self.schema.version,
            python_imports=imports.imports,
            base_model=base_model,
            injected_classes=injected_classes,
            enums=enums,
            classes=classes,
        )
        module = self.include_metadata(module, self.schemaview.schema)
        module = self.before_render_template(module, self.schemaview)
        return module

    def serialize(self) -> str:
        module = self.render()
        serialized = module.render(self._template_environment(), self.black)
        serialized = self.after_render_template(serialized, self.schemaview)
        return serialized

    def default_value_for_type(self, typ: str) -> str:
        return "None"


def _subclasses(cls: Type):
    return set(cls.__subclasses__()).union([s for c in cls.__subclasses__() for s in _subclasses(c)])


_TEMPLATE_NAMES = sorted(list(set([c.template for c in _subclasses(PydanticTemplateModel)])))


@shared_arguments(PydanticGenerator)
@click.option("--template-file", hidden=True)
@click.option(
    "--template-dir",
    type=click.Path(),
    help="""
Optional jinja2 template directory to use for class generation.

Pass a directory containing templates with the same name as any of the default 
:class:`.PydanticTemplateModel` templates to override them. The given directory will be 
searched for matching templates, and use the default templates as a fallback 
if an override is not found
  
Available templates to override:

\b
"""
    + "\n".join(["- " + name for name in _TEMPLATE_NAMES]),
)
@click.option(
    "--array-representations",
    type=click.Choice([k.value for k in ArrayRepresentation]),
    multiple=True,
    default=["list"],
    help="List of array representations to accept for array slots. Default is list of lists.",
)
@click.option(
    "--extra-fields",
    type=click.Choice(["allow", "ignore", "forbid"], case_sensitive=False),
    default="forbid",
    help="How to handle extra fields in BaseModel.",
)
@click.option(
    "--black",
    is_flag=True,
    default=False,
    help="Format generated models with black (must be present in the environment)",
)
@click.option(
    "--meta",
    type=click.Choice([k for k in MetadataMode]),
    default="auto",
    help="How to include linkml schema metadata in generated pydantic classes. "
    "See docs for MetadataMode for full description of choices. "
    "Default (auto) is to include all metadata that can't be otherwise represented",
)
@click.version_option(__version__, "-V", "--version")
@click.command()
def cli(
    yamlfile,
    template_file=None,
    template_dir: Optional[str] = None,
    head=True,
    genmeta=False,
    classvars=True,
    slots=True,
    array_representations=list("list"),
    extra_fields: Literal["allow", "forbid", "ignore"] = "forbid",
    black: bool = False,
    meta: MetadataMode = "auto",
    **args,
):
    """Generate pydantic classes to represent a LinkML model"""
    if template_file is not None:
        raise DeprecationWarning(
            (
                "Passing a single template_file is deprecated. Pass a directory of template files instead. "
                "See help string for --template-dir"
            )
        )

    if template_dir is not None:
        if not Path(template_dir).exists():
            raise FileNotFoundError(f"The template directory {template_dir} does not exist!")

    gen = PydanticGenerator(
        yamlfile,
        array_representations=[ArrayRepresentation(x) for x in array_representations],
        extra_fields=extra_fields,
        emit_metadata=head,
        genmeta=genmeta,
        gen_classvars=classvars,
        gen_slots=slots,
        template_dir=template_dir,
        black=black,
        metadata_mode=meta,
        **args,
    )
    print(gen.serialize())


if __name__ == "__main__":
    cli()
