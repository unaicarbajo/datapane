# flake8: noqa:F811
from __future__ import annotations

import dataclasses as dc
import typing as t
from collections import namedtuple

pass

from lxml import etree
from lxml.builder import ElementMaker
from multimethod import DispatchError, multimethod

from datapane import DPClientError
from datapane.blocks import BaseElement
from datapane.blocks.asset import AssetBlock
from datapane.blocks.interactive import Interactive, TargetMode, gen_name
from datapane.blocks.layout import ContainerBlock
from datapane.blocks.text import EmbeddedTextBlock
from datapane.common.viewxml_utils import mk_attribs
from datapane.view.view import View
from datapane.view.visitors import ViewVisitor

if t.TYPE_CHECKING:
    from datapane.processors import FileEntry, FileStore

    # from typing_extensions import "XMLBuilder"

E = ElementMaker()  # XML Tag Factory


@dc.dataclass
class XMLBuilder(ViewVisitor):
    """Hold state whilst building the Report XML document"""

    dispatch_to: t.ClassVar[str] = "as_xml"

    store: FileStore
    # element: t.Optional[etree.Element] = None  # Empty Group Element?
    elements: t.List[etree.Element] = dc.field(default_factory=list)

    @property
    def store_count(self) -> int:
        return len(self.store.files)

    def add_element(self, _: BaseElement, e: etree.Element) -> XMLBuilder:
        """Add an element to the list of nodes at the current XML tree location"""
        self.elements.append(e)
        return self

    # xml convertors
    @multimethod
    def visit(self, b: BaseElement) -> XMLBuilder:
        """Base implementation - just created an empty tag including all the initial attributes"""
        _E = getattr(E, b._tag)
        return self.add_element(b, _E(**b._attributes))

    @multimethod
    def visit(self, b: ContainerBlock) -> XMLBuilder:
        cur_elemnts = self.elements
        self.elements = []
        b.traverse(self)  # visit subnodes
        # reduce(lambda _s, block: block.accept(_s), b.blocks, self)
        # build the element
        _E = getattr(E, b._tag)
        element = _E(*self.elements, **b._attributes)
        self.elements = cur_elemnts
        return self.add_element(b, element)

    @multimethod
    def visit(self, b: View) -> XMLBuilder:
        # we should only ever be at the start of a view
        assert len(self.elements) == 0

        b.traverse(self)  # visit subnodes

        # create top-level structure
        view_doc = E.View(
            # E.Internal(),
            *self.elements,
            **mk_attribs(version="1", fragment=b.fragment),
        )
        self.elements = [view_doc]
        return self

    @multimethod
    def visit(self, b: EmbeddedTextBlock) -> XMLBuilder:
        # NOTE - do we use etree.CDATA wrapper?
        _E = getattr(E, b._tag)
        return self.add_element(b, _E(etree.CDATA(b.content), **b._attributes))

    @multimethod
    def visit(self, b: Interactive) -> XMLBuilder:
        c_e = b.controls._to_xml()

        # Special Target handling - this occurs at the lower IR level for now,
        # should move to OptimiseAST pass
        if b.target == TargetMode.SELF:
            name = gen_name()
            e = E.Interactive(c_e, **{**b._attributes, "target": name, "name": name})
        elif b.target in (TargetMode.BELOW, TargetMode.SIDE):
            # desugar to create a Group(Interactive, Result)
            cols = "1" if b.target == TargetMode.BELOW else "2"
            name = gen_name()
            e = E.Group(
                E.Interactive(c_e, **{**b._attributes, "target": name}),
                E.Group(E.Empty(name=name), columns="1"),
                columns=cols,
            )
        else:
            # use default target
            e = E.Interactive(c_e, **b._attributes)

        return self.add_element(b, e)

    @multimethod
    def visit(self, b: AssetBlock):
        """Main XMl creation method - visitor method"""
        fe = self._add_asset_to_store(b)

        _E = getattr(E, b._tag)

        e: etree._Element = _E(
            type=fe.mime,
            # size=conv_attrib(fe.size),
            # hash=fe.hash,
            **{**b._attributes, **b.get_file_attribs()},
            # src=f"attachment://{self.store_count}",
            src=f"ref://{fe.hash}",
        )

        if b.caption:
            e.set("caption", b.caption)
        return self.add_element(b, e)

    def _add_asset_to_store(self, b: AssetBlock) -> FileEntry:
        """Default asset store handler that operates on native Python objects"""
        # import here as a very slow module due to nested imports
        # from .. import files

        # check if we already have stored this asset to the store
        # TODO - do we just persist the asset store across the session??
        if b._prev_entry and type(b._prev_entry) == self.store.fw_klass:
            self.store.add_file(b._prev_entry)
            return b._prev_entry

        if b.data is not None:
            # fe = files.add_to_store(self.data, s.store)
            try:
                writer = get_writer(b)
                meta: AssetMeta = writer.get_meta(b.data)
                fe = self.store.get_file(meta.ext, meta.mime)
                writer.write_file(b.data, fe.file)
                self.store.add_file(fe)
            except DispatchError:
                raise DPClientError(f"{type(b.data).__name__} not supported for {self.__class__.__name__}")
        elif b.file is not None:
            fe = self.store.load_file(b.file)
        else:
            raise DPClientError("No asset to add")

        b._prev_entry = fe
        return fe


AssetMeta = namedtuple("AssetMeta", "ext mime")


class AssetWriterP(t.Protocol):
    """Implement these in any class to support asset writing
    for a particular AssetBlock"""

    def get_meta(self, x: t.Any) -> AssetMeta:
        ...

    def write_file(self, x: t.Any, f) -> None:
        ...


asset_mapping: t.Dict[t.Type[AssetBlock], t.Type[AssetWriterP]] = dict()


def get_writer(b: AssetBlock) -> AssetWriterP:
    import datapane.blocks.asset as a

    from . import asset_writers as aw

    if not asset_mapping:
        asset_mapping.update(
            {
                a.Plot: aw.PlotWriter,
                a.Table: aw.HTMLTableWriter,
                a.DataTable: aw.DataTableWriter,
                a.Attachment: aw.AttachmentWriter,
            }
        )
    return asset_mapping[type(b)]()