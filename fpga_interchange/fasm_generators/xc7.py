#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2020  The SymbiFlow Authors.
#
# Use of this source code is governed by a ISC-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/ISC
#
# SPDX-License-Identifier: ISC
"""
This file defines the Series-7 devices FASM generator class.

The FASM generator extends the generic one, with additional
functions and handlers for specific elements in the Series-7 devices.

The ultimate goal is to have most of the FASM annotations included in the
device resources, so that the size of this file can be reduced to handle
only very specific cases which are hard to encode in the device database.

Such special cases may include:
    - PLL and MMCM register configuration functions
    - Extra features corresponding to specific PIPs (such as BUFG rebuf)

"""
import re
from collections import namedtuple
from enum import Enum
from itertools import product

from fpga_interchange.fasm_generators.generic import FasmGenerator, PhysCellInstance, invert_bitstring
from fpga_interchange.fasm_generators.xc7_iobs import iob_settings
from fpga_interchange.route_stitching import flatten_segments
from fpga_interchange.physical_netlist import PhysicalPip, Pin
"""
This is a helper object that is used to find and emit extra features
that do depend on the usage of specific PIPs or Pseudo PIPs.

regex: is used to identify the correct PIPs.
features: list of extra features to be added.
callback: function to get the correct prefix for the feature, based on the
          regex match results.
"""
ExtraFeatures = namedtuple('ExtraFeatures', 'regex features callback')

VCC_NET = "GLOBAL_LOGIC1"
GND_NET = "GLOBAL_LOGIC0"


class LutsEnum(Enum):
    LUT5 = 0
    LUT6 = 1

    @classmethod
    def from_str(cls, label):
        if label == "LUT5":
            return cls.LUT5
        elif label == "LUT6":
            return cls.LUT6
        else:
            raise NotImplementedError


def parse_lut_bel(lut_bel):
    """
    Perform a regex match with the provided LUT name, to identify in which category
    the LUT falls into:

    input:
        lut_name = A5LUT

    output:
        lut_type = LUT5
        lut_loc  = A
    """

    bel_re = re.compile("([ABCD])([56])LUT")

    m = bel_re.match(lut_bel)
    assert m, lut_bel

    # A, B, C or D
    lut_loc = m.group(1)

    # LUT5 or LUT6
    lut_type = "LUT{}".format(m.group(2))

    return lut_loc, lut_type


class XC7FasmGenerator(FasmGenerator):
    def handle_brams(self):
        """
        Handles slice RAMB18 FASM feature emission.
        """

        # TODO: handle ramb36 as well

        init_re = re.compile("INITP?_[0-9][0-9]")

        z_features = ["INIT_A", "INIT_B", "SRVAL_A", "SRVAL_B"]
        str_features = [
            "RDADDR_COLLISION_HWCONFIG", "RSTREG_PRIORITY_A",
            "RSTREG_PRIORITY_B", "WRITE_MODE_A", "WRITE_MODE_B"
        ]
        rw_widths = [
            "READ_WIDTH_A", "READ_WIDTH_B", "WRITE_WIDTH_A", "WRITE_WIDTH_B"
        ]

        allowed_cell_types = ["RAMB18E1"]
        allowed_site_types = ["RAMB18E1"]

        for cell_instance, cell_data in self.physical_cells_instances.items():
            cell_type = cell_data.cell_type
            if cell_type not in allowed_cell_types:
                continue

            tile_name = cell_data.tile_name
            tile_type = cell_data.tile_type
            site_name = cell_data.site_name
            bram_prefix = self.get_bram_prefix(site_name, tile_type)

            is_y1 = "Y1" in bram_prefix

            self.add_cell_feature((tile_name, bram_prefix, "IN_USE"))

            attributes = cell_data.attributes

            fasm_features = list()
            ram_mode = attributes["RAM_MODE"]
            for attr, value in attributes.items():
                init_param = self.device_resources.get_parameter_definition(
                    cell_type, attr)

                init_match = init_re.match(attr)
                fasm_feature = None
                if init_match:
                    init_value = init_param.decode_integer(value)

                    if init_value == 0:
                        continue

                    init_str_value = "{:b}".format(init_value)
                    init_str = "{len}'b{value}".format(
                        len=len(init_str_value), value=init_str_value)
                    fasm_feature = "{}[{}:0]={}".format(
                        attr,
                        len(init_str_value) - 1, init_str)
                    fasm_features.append(fasm_feature)

                elif attr in z_features:
                    init_value = init_param.decode_integer(value)
                    width = init_param.width

                    init_str_value = "{value:0{width}b}".format(
                        value=init_value, width=width)

                    feature_value = invert_bitstring(init_str_value)

                    fasm_feature = "Z{}[{}:0]={}'b{}".format(
                        attr, width - 1, width, feature_value)
                    fasm_features.append(fasm_feature)

                elif attr in str_features:
                    fasm_features.append("{}_{}".format(attr, value))

                elif attr in rw_widths:
                    init_value = init_param.decode_integer(value)

                    # Handle special INIT value case
                    if is_y1 and init_value == 36 and ram_mode == "SDP" and attr == "READ_WIDTH_A":
                        init_value = 18

                    fasm_features.append("{}_{}".format(attr, init_value))

                    if init_value == 36 and ram_mode == "SDP":
                        fasm_features.append("SDP_{}_36".format(attr))

            for fasm_feature in fasm_features:
                self.add_cell_feature((tile_name, bram_prefix, fasm_feature))

            if not is_y1:
                for feature in ["ALMOST_EMPTY_OFFSET", "ALMOST_FULL_OFFSET"]:
                    value = "1" * 13
                    fasm_feature = "Z{}[12:0]=13'b{}".format(feature, value)
                    self.add_cell_feature((tile_name, fasm_feature))

    def handle_ios(self):
        """
        This function is specialized to add FASM features for the IO buffers
        in the 7-Series database format.
        """

        import sys

        # FIXME: Need to make this dynamic, and find a suitable way to add FASM annotations to the device resources.
        #        In addition, a reformat of the database might be required to have an easier handling of these
        #        features.
        allowed_io_types = ["IBUF", "OBUF", "OBUFT", "OBUFTDS"]

        iob_sites = ["IOB_Y0", "IOB_Y1"]
        iob_re = re.compile("IOB_X[0-9]+Y([0-9]+)")

        iob_instances = {}

        for cell_data in self.physical_cells_instances.values():
            cell_type = cell_data.cell_type
            if cell_type not in allowed_io_types:
                continue

            site_name = cell_data.site_name
            tile_name = cell_data.tile_name
            attrs = cell_data.attributes

            if site_name not in iob_instances:
                iob_instances[site_name] = (attrs, tile_name, False, False)

            attrs, tile_name, is_input, is_output = iob_instances[site_name]

            if cell_type.startswith("O"):
                is_output = True

            if cell_type.startswith("I"):
                is_input = True

            iob_instances[site_name] = (attrs, tile_name, is_input, is_output)

        for site_name, (attrs, tile_name, is_input,
                        is_output) in iob_instances.items():

            iostandard = attrs.get("IOSTANDARD", "LVCMOS33")
            drive = int(attrs.get("DRIVE", "12"))
            slew = attrs.get("SLEW", "SLOW")

            is_inout = is_input and is_output
            is_only_in = is_input and not is_output

            m = iob_re.match(site_name)
            assert m, site_name

            y_coord = int(m.group(1))
            if "SING" in tile_name and y_coord % 50 == 0:
                iob_sites_idx = 0
            elif "SING" in tile_name and y_coord % 50 == 49:
                iob_sites_idx = 1
            else:
                iob_sites_idx = y_coord % 2

            iob_site = iob_sites[iob_sites_idx]

            for feature, settings in iob_settings.items():
                if feature.endswith("IN_ONLY") and is_output:
                    continue

                if ("DRIVE" in feature or "SLEW" in feature) and is_only_in:
                    continue

                if (feature.endswith("IN")
                        or feature.endswith("IN_DIFF")) and not is_input:
                    continue

                iostandards = settings["iostandards"]
                slews = settings["slews"]

                if len(iostandards) != 0 and iostandard not in iostandards:
                    continue

                drives = iostandards[iostandard]
                if len(drives) != 0 and drive not in drives:
                    continue

                if len(slews) != 0 and slew not in slews:
                    continue

                self.add_cell_feature((tile_name, iob_site, feature))

            pulltype = attrs.get("PULLTYPE", "NONE")
            self.add_cell_feature((tile_name, iob_site, "PULLTYPE", pulltype))

            if iostandard.startswith("DIFF_") and is_output:
                self.add_cell_feature((tile_name, "OUT_DIFF"))

    @staticmethod
    def get_slice_prefix(site_name, tile_type):
        """
        Returns the slice prefix corresponding to the input site name.
        """

        slice_sites = {
            "CLBLL_L": ["SLICEL_X0", "SLICEL_X1"],
            "CLBLL_R": ["SLICEL_X0", "SLICEL_X1"],
            "CLBLM_L": ["SLICEM_X0", "SLICEL_X1"],
            "CLBLM_R": ["SLICEM_X0", "SLICEL_X1"],
        }

        slice_re = re.compile("SLICE_X([0-9]+)Y[0-9]+")
        m = slice_re.match(site_name)
        assert m, site_name

        slice_site_idx = int(m.group(1)) % 2
        return slice_sites[tile_type][slice_site_idx]

    @staticmethod
    def get_bram_prefix(site_name, tile_type):
        """
        Returns the slice prefix corresponding to the input site name.
        """

        ramb_re = re.compile("RAMB18_X[0-9]+Y([0-9]+)")
        m = ramb_re.match(site_name)
        assert m, site_name

        ramb_site_idx = int(m.group(1)) % 2
        return "RAMB18_Y{}".format(ramb_site_idx)

    def add_lut_features(self):
        for lut in self.luts.values():
            tile_name, slice_site, lut_name = lut["data"]

            lut5 = lut[LutsEnum.LUT5]
            lut6 = lut[LutsEnum.LUT6]

            if lut5 is not None and lut6 is not None:
                lut_init = "{}{}".format(lut6[0:32], lut5[32:64])
            elif lut5 is not None:
                lut_init = lut5[32:64].zfill(32)
            elif lut6 is not None:
                lut_init = lut6
            else:
                assert False

            init_feature = "INIT[{}:0]={}'b{}".format(
                len(lut_init) - 1, len(lut_init), lut_init)

            self.add_cell_feature((tile_name, slice_site, lut_name,
                                   init_feature))

    def handle_luts(self):
        """
        This function handles LUTs FASM features generation
        """

        self.luts = dict()

        for cell_instance, cell_data in self.physical_cells_instances.items():
            if not cell_data.cell_type.startswith("LUT"):
                continue

            site_name = cell_data.site_name
            site_type = cell_data.site_type

            tile_name = cell_data.tile_name
            tile_type = cell_data.tile_type
            slice_site = self.get_slice_prefix(site_name, tile_type)

            bel = cell_data.bel
            lut_loc, lut_type = parse_lut_bel(bel)
            lut_name = "{}LUT".format(lut_loc)

            init_param = self.device_resources.get_parameter_definition(
                cell_data.cell_type, "INIT")
            init_value = init_param.decode_integer(
                cell_data.attributes["INIT"])

            phys_lut_init = self.lut_mapper.get_phys_cell_lut_init(
                init_value, cell_data)

            key = (site_name, lut_loc)
            if key not in self.luts:
                self.luts[key] = {
                    "data": (tile_name, slice_site, lut_name),
                    LutsEnum.LUT5: None,
                    LutsEnum.LUT6: None,
                }

            self.luts[key][LutsEnum.from_str(lut_type)] = phys_lut_init

    def handle_slice_ff(self):
        """
        Handles slice FFs FASM feature emission.
        """

        allowed_cell_types = ["FDRE", "FDSE", "FDCE", "FDPE", "LDCE", "LDPE"]
        allowed_site_types = ["SLICEL", "SLICEM"]

        for cell_instance, cell_data in self.physical_cells_instances.items():
            cell_type = cell_data.cell_type
            if cell_type not in allowed_cell_types:
                continue

            site_name = cell_data.site_name
            site_type = cell_data.site_type

            if site_type not in allowed_site_types:
                continue

            tile_name = cell_data.tile_name
            tile_type = cell_data.tile_type
            slice_site = self.get_slice_prefix(site_name, tile_type)

            bel = cell_data.bel

            if cell_type in ["FDRE", "FDCE", "LDCE"]:
                self.add_cell_feature((tile_name, slice_site, bel, "ZRST"))

            if cell_type.startswith("LD"):
                self.add_cell_feature((tile_name, slice_site, "LATCH"))

            if cell_type in ["FDRE", "FDSE"]:
                self.add_cell_feature((tile_name, slice_site, "FFSYNC"))

            init_param = self.device_resources.get_parameter_definition(
                cell_data.cell_type, "INIT")
            init_value = init_param.decode_integer(
                cell_data.attributes["INIT"])

            if init_value == 0:
                self.add_cell_feature((tile_name, slice_site, bel, "ZINI"))

    def handle_clock_resources(self):

        bufg_re = re.compile("BUFGCTRL_X[0-9]+Y([0-9]+)")
        for cell_instance, cell_data in self.physical_cells_instances.items():
            cell_type = cell_data.cell_type
            if cell_type not in ["BUFG", "BUFGCTRL"]:
                continue

            site_name = cell_data.site_name
            site_type = cell_data.site_type

            m = bufg_re.match(cell_data.site_name)
            assert m, site_name

            site_loc = int(m.group(1)) % 16
            site_prefix = "BUFGCTRL.BUFGCTRL_X0Y{}".format(site_loc)

            tile_name = cell_data.tile_name

            self.add_cell_feature((tile_name, site_prefix, "IN_USE"))

            if cell_type == "BUFG":
                for feature in ["IS_IGNORE1_INVERTED", "ZINV_CE0", "ZINV_S0"]:
                    self.add_cell_feature((tile_name, site_prefix, feature))

    def handle_slice_routing_bels(self):
        tile_types = ["CLBLL_L", "CLBLL_R", "CLBLM_L", "CLBLM_R"]
        routing_bels = self.get_routing_bels(tile_types)

        used_muxes = ["SRUSEDMUX", "CEUSEDMUX"]

        excluded_bels = [
            "{}USED".format(bel) for bel in ["A", "B", "C", "D", "COUT"]
        ]
        excluded_bels += ["CLKINV"]

        carry_cy = ["{}CY0".format(bel) for bel in ["A", "B", "C", "D"]]

        for site, bel, pin, _ in routing_bels:
            if bel in excluded_bels:
                continue

            tile_name, tile_type = self.get_tile_info_at_site(site)
            slice_prefix = self.get_slice_prefix(site, tile_type)

            if bel in used_muxes:
                if pin in ["0", "1"]:
                    continue
                feature = (tile_name, slice_prefix, bel)
            elif bel in carry_cy:
                # TODO: requires possible adjustment to the FASM database format
                if pin != "O5":
                    continue
                feature = (tile_name, slice_prefix, "CARRY4", bel)
            elif bel == "PRECYINIT":
                if pin == "1":
                    # TODO: requires possible adjustment to the FASM database format
                    pin = "C{}".format(pin)
                elif pin == "0":
                    # default value, do not emit as might collide with CIN
                    continue
                feature = (tile_name, slice_prefix, bel, pin)
            else:
                feature = (tile_name, slice_prefix, bel, pin)

            self.add_cell_feature(feature)

    def handle_bram_routing_bels(self):
        tile_types = ["BRAM_L", "BRAM_R"]
        routing_bels = self.get_routing_bels(tile_types)

        for site, bel, pin, is_inverting in routing_bels:
            tile_name, tile_type = self.get_tile_info_at_site(site)
            bram_prefix = self.get_bram_prefix(site, tile_type)

            if not is_inverting:
                zinv_feature = "ZINV_{}".format(pin)
                self.add_cell_feature((tile_name, bram_prefix, zinv_feature))

    def handle_slice_bel_pins(self):
        """
        This function handles the addition of special features when specific BEL
        pins are used
        """
        tile_types = ["CLBLL_L", "CLBLL_R", "CLBLM_L", "CLBLM_R"]
        bel_pins = self.get_bel_pins_annotation(tile_types)

        for site, bel, pin in bel_pins:

            tile_name, tile_type = self.get_tile_info_at_site(site)
            slice_prefix = self.get_slice_prefix(site, tile_type)

            if bel == "CARRY4" and pin == "CIN":
                self.add_cell_feature((tile_name, slice_prefix,
                                       "PRECYINIT.CIN"))

    def handle_site_thru(self, site_thru_pips):
        """
        This function is currently specialized to add very specific features
        for pseudo PIPs which need to be enabled to get the correct HW behaviour
        """

        def get_feature_prefix(site_thru_feature, wire):
            regex = re.compile(site_thru_feature.regex)

            m = regex.match(wire)

            return site_thru_feature.callback(m) if m else None

        # FIXME: this information needs to be added as an annotation
        #        to the device resources
        site_thru_features = list()
        site_thru_features.append(
            ExtraFeatures(
                regex="IOI_OLOGIC([01])_D1",
                features=["OMUX.D1", "OQUSED", "OSERDES.DATA_RATE_TQ.BUF"],
                callback=lambda m: "OLOGIC_Y{}".format(m.group(1))))
        site_thru_features.append(
            ExtraFeatures(
                regex="IOI_OLOGIC([01])_T1",
                features=["ZINV_T1"],
                callback=lambda m: "OLOGIC_Y{}".format(m.group(1))))
        site_thru_features.append(
            ExtraFeatures(
                regex="[LR]IOI_ILOGIC([01])_D",
                features=["ZINV_D"],
                callback=lambda m: "ILOGIC_Y{}".format(m.group(1))))
        site_thru_features.append(
            ExtraFeatures(
                regex="CLK_HROW_CK_MUX_OUT_([LR])([0-9]+)",
                features=["IN_USE", "ZINV_CE"],
                callback=lambda m: "BUFHCE.BUFHCE_X{}Y{}".format(0 if m.group(1) == "L" else 1, m.group(2))))
        #TODO: Better handle BUFGCTRL route-through depending on the
        #      used input pin
        site_thru_features.append(
            ExtraFeatures(
                regex="CLK_BUFG_BUFGCTRL([0-9]+)_I0",
                features=[
                    "IN_USE", "ZINV_CE0", "ZINV_S0", "IS_IGNORE1_INVERTED"
                ],
                callback=
                lambda m: "BUFGCTRL.BUFGCTRL_X0Y{}".format(m.group(1))))
        site_thru_features.append(
            ExtraFeatures(
                regex="CLK_BUFG_BUFGCTRL([0-9]+)_I1",
                features=[
                    "IN_USE", "ZINV_CE1", "ZINV_S1", "IS_IGNORE0_INVERTED"
                ],
                callback=
                lambda m: "BUFGCTRL.BUFGCTRL_X0Y{}".format(m.group(1))))

        for tile, wire0, wire1 in site_thru_pips:
            for site_thru_feature in site_thru_features:
                prefix = get_feature_prefix(site_thru_feature, wire0)

                if prefix is None:
                    continue

                for feature in site_thru_feature.features:
                    self.add_cell_feature((tile, prefix, feature))

                break

    def handle_lut_thru(self, lut_thru_pips):
        for (net_name, site, bel), pin in lut_thru_pips.items():
            pin_name = pin["pin_name"]
            is_valid = pin["is_valid"]

            if not is_valid:
                continue

            lut_loc, lut_type = parse_lut_bel(bel)

            site_type = list(
                self.device_resources.site_name_to_site[site].keys())[0]

            lut_key = (site, lut_loc)
            if lut_key not in self.luts:
                tile_name, tile_type = self.get_tile_info_at_site(site)
                slice_site = self.get_slice_prefix(site, tile_type)
                lut_name = "{}LUT".format(lut_loc)

                self.luts[lut_key] = {
                    "data": (tile_name, slice_site, lut_name),
                    LutsEnum.LUT5: None,
                    LutsEnum.LUT6: None,
                }

            lut_enum = LutsEnum.from_str(lut_type)
            assert self.luts[lut_key][lut_enum] is None, (net_name, site, bel)

            if net_name == VCC_NET:
                lut_init = self.lut_mapper.get_const_lut_init(
                    1, site_type, bel)
            elif net_name == GND_NET:
                lut_init = self.lut_mapper.get_const_lut_init(
                    0, site_type, bel)
            else:
                lut_init = self.lut_mapper.get_phys_wire_lut_init(
                    2, site_type, "LUT1", bel, pin_name)

            self.luts[lut_key][lut_enum] = lut_init

    def handle_extra_pip_features(self, extra_pip_features):
        """
        There are some sensitive PIPs and features which mostly deal with clocking resources
        and require special handling, as without the extra features, the clocking tree configuration
        migth be incomplete, resulting in unexpected hardware behavior.

        This function handles all the special extra features corresponding to a restricted class
        of special PIPs.
        """

        def run_regex_match(extra_feature,
                            tile,
                            wire,
                            extra_wires,
                            enable_extra_wires=True):
            """
            This helper function adds the corresponding extra features based on the callback of
            the ExtraFeature element and the regex match status.

            It also adds, if enabled, a set of extra wires belonging to the input wire's
            node, that may require some extra features as well.
            """

            extra_wires_to_check = ["CLK_BUFG_REBUF_R_CK", "HCLK_CK_BUFHCLK"]

            if any(
                    wire.startswith(extra_wire) for extra_wire in
                    extra_wires_to_check) and enable_extra_wires:
                node_index = self.device_resources.node(tile, wire).node_index
                wires = self.device_resources.device_resource_capnp.nodes[
                    node_index].wires
                for wire_idx in wires:
                    tile_wire = self.device_resources.device_resource_capnp.wires[
                        wire_idx]
                    wire_name = self.device_resources.strs[tile_wire.wire]
                    tile_name = self.device_resources.strs[tile_wire.tile]

                    extra_wires.add((tile_name, wire_name))

            regex = re.compile(extra_feature.regex)
            m = regex.match(wire)

            if m:
                prefix = extra_feature.callback(m)

                for f in extra_feature.features:
                    f = "{}{}".format(prefix, f)
                    self.add_cell_feature((tile, f))

        # TODO: The FASM database should be reformatted so to have more
        #       regular extra PIP features.
        regexs = list()
        regexs.append(
            ExtraFeatures(
                regex="(CLK_HROW_CK_IN_[LR][0-9]+)",
                features=["_ACTIVE"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="(CLK_HROW_R_CK_GCLK[0-9]+)",
                features=["_ACTIVE"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="(HCLK_CMT_CCIO[0-9]+)",
                features=["_ACTIVE", "_USED"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="(HCLK_CMT_CK_BUFHCLK[0-9]+)",
                features=["_USED"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="CLK_BUFG_REBUF_R_CK_(GCLK[0-9]+)_BOT",
                features=["_ENABLE_ABOVE"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="CLK_BUFG_REBUF_R_CK_(GCLK[0-9]+)_TOP",
                features=["_ENABLE_BELOW"],
                callback=lambda m: m.group(1)))
        regexs.append(
            ExtraFeatures(
                regex="(HCLK_CK_BUFHCLK[0-9]+)",
                features=[""],
                callback=lambda m: "ENABLE_BUFFER.{}".format(m.group(1))))
        regexs.append(
            ExtraFeatures(
                regex="BRAM_CASCOUT_ADDR(ARD|BWR)ADDR",
                features=[""],
                callback=lambda m: "CASCOUT_{}_ACTIVE".format(m.group(1))))

        extra_wires = set()
        for tile_pips in extra_pip_features.values():
            for tile, wire in tile_pips:
                for extra_feature in regexs:
                    run_regex_match(extra_feature, tile, wire, extra_wires)

        # Handle extra wires
        exclude_tiles = [
            "HCLK_L", "HCLK_R", "HCLK_L_BOT_UTURN", "HCLK_R_BOT_UTURN"
        ]
        for tile, wire in extra_wires:
            if any(
                    tile.startswith(exclude_tile)
                    for exclude_tile in exclude_tiles):
                continue

            for extra_feature in regexs:
                run_regex_match(extra_feature, tile, wire, extra_wires, False)

    def handle_routes(self):
        """
        Handles all the FASM features corresponding to PIPs

        In addition, emits extra features necessary to have the clock
        resources working properly, as well the emission of site route
        thru features for pseudo PIPs
        """

        avail_lut_thrus = list()
        for _, _, _, _, bel, bel_type in self.device_resources.yield_bels():
            if bel_type in ["LUT5", "LUT6"]:
                avail_lut_thrus.append(bel)

        bel_pins = [("CARRY4", "CIN")]

        tile_types = [
            "HCLK_L", "HCLK_R", "HCLK_L_BOT_UTURN", "HCLK_R_BOT_UTURN",
            "HCLK_CMT", "HCLK_CMT_L", "CLK_HROW_TOP_R", "CLK_HROW_BOT_R",
            "CLK_BUFG_REBUF", "BRAM_L", "BRAM_R"
        ]
        extra_pip_features = dict(
            (tile_type, set()) for tile_type in tile_types)

        pip_feature_format = "{tile}.{wire1}.{wire0}"
        site_thru_pips, lut_thru_pips = self.fill_pip_features(
            pip_feature_format, extra_pip_features, avail_lut_thrus, bel_pins)

        self.handle_extra_pip_features(extra_pip_features)
        self.handle_site_thru(site_thru_pips)
        self.handle_lut_thru(lut_thru_pips)

    def fill_features(self):
        # Handling BELs
        self.handle_brams()
        self.handle_clock_resources()
        self.handle_ios()
        self.handle_luts()
        self.handle_slice_ff()

        # Handling PIPs and Route-throughs
        self.handle_routes()

        # Handling routing BELs
        self.handle_slice_routing_bels()
        self.handle_bram_routing_bels()

        # Handling bel pins
        self.handle_slice_bel_pins()

        # Emit LUT features. This needs to be done at last as
        # LUT features depend also on LUT route-thru
        self.add_lut_features()
