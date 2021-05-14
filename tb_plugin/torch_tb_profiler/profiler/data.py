# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# --------------------------------------------------------------------------
import gzip
import io as sysio
import json
import re
import tempfile
from collections import OrderedDict

from .. import io, utils
from . import trace
from .kernel_parser import KernelParser
from .module_parser import ModuleParser
from .node_parser import NodeContext, NodeParser
from .overall_parser import OverallParser
from .step_parser import ProfileRole, StepParser

logger = utils.get_logger()


class RunData(object):
    def __init__(self, name, run_dir):
        self.name = name
        self.run_dir = run_dir
        self.profiles = OrderedDict()

class RunProfileData(object):
    def __init__(self, worker):
        self.worker = worker
        self.data_schema_version = None
        self.events = None
        self.trace_file_path = None
        self.has_runtime = False
        self.has_kernel = False
        self.has_communication = False
        self.has_memcpy_or_memset = False
        self.steps_costs = None
        self.steps_names = None
        self.avg_costs = None
        self.op_list_groupby_name = None
        self.op_list_groupby_name_input = None
        self.stack_lists_group_by_name = None
        self.stack_lists_group_by_name_input = None
        self.kernel_list_groupby_name_op = None
        self.kernel_stat = None
        self.recommendations = []
        self.comm_node_list = []
        self.comm_overlap_costs = None
        self.total_comm_stats = dict()
        self.step_comm_stats = dict()

    @staticmethod
    def parse(run_dir, worker, path, caches):
        logger.debug("Parse trace, run_dir=%s, worker=%s", run_dir, path)

        trace_path, trace_json= RunProfileData._preprocess_file(caches, io.join(run_dir, path))

        profile = RunProfileData(worker)
        profile.trace_file_path = trace_path
        if type(trace_json) is dict:
            metadata = trace_json.get("profilerMetadata", None)
            version = metadata.get("DataSchemaVersion") if metadata else None
            profile.data_schema_version = version
            trace_json = trace_json["traceEvents"]

        profile.events = []
        for data in trace_json:
            event = trace.create_event(data)
            if event is not None:
                profile.events.append(event)

        return profile

    @staticmethod
    def _preprocess_file(caches, trace_path):
        if not io.exists(trace_path):
            raise FileNotFoundError(trace_path)

        data = caches.read(trace_path)
        if trace_path.endswith('.gz'):
            data = gzip.decompress(data)

        try:
            trace_json = json.loads(data)
        except json.decoder.JSONDecodeError as e:
            # Kineto may export json file with non-ascii code. before this is fixed, use a workaround
            # to handle JSONDecodeError, re-encode it and save to a temp file
            try:
                trace_json = json.loads(data, strict=False)
            except json.decoder.JSONDecodeError:
                with sysio.StringIO() as fout:
                    str_data = data.decode("utf-8")
                    # only replace the N/A without surrounding double quote
                    fout.write(re.sub(r'(?<!")N/A(?!")', "\"N/A\"", str_data))
                    trace_json = json.loads(fout.getvalue())

            fp = tempfile.NamedTemporaryFile('w+t', suffix='.json.gz', delete=False)
            fp.close()
            with gzip.open(fp.name, mode='wt') as fzip:
                fzip.write(json.dumps(trace_json))
            logger.warning("Get JSONDecodeError: %s, Re-encode it to temp file: %s", e.msg, fp.name)
            trace_path = fp.name
            caches.add_tempfile(fp.name)

        return trace_path, trace_json

    def process(self):

        node_parser = NodeParser()
        node_context = NodeContext()
        node_parser.parse_events(self.events, node_context)

        step_parser = StepParser()
        step_parser.parse_events(self.events, node_parser)
        
        logger.debug("ModuleParser")
        module_parser = ModuleParser()
        module_parser.aggregate(node_context)
        self.op_list_groupby_name = module_parser.op_list_groupby_name
        self.op_list_groupby_name_input = module_parser.op_list_groupby_name_input
        self.stack_lists_group_by_name = module_parser.stack_lists_group_by_name
        self.stack_lists_group_by_name_input = module_parser.stack_lists_group_by_name_input
        self.kernel_list_groupby_name_op = module_parser.kernel_list_groupby_name_op

        logger.debug("OverallParser")
        overall_parser = OverallParser()
        overall_parser.aggregate(node_parser, step_parser)
        self.has_runtime = step_parser.has_runtime
        self.has_kernel = step_parser.has_kernel
        self.has_communication = step_parser.has_communication
        self.has_memcpy_or_memset = step_parser.has_memcpy_or_memset
        self.steps_costs = overall_parser.steps_costs
        self.steps_names = step_parser.steps_names
        self.avg_costs = overall_parser.avg_costs
        self.comm_node_list = node_parser.comm_node_list
        self.comm_overlap_costs = overall_parser.communication_overlap

        if self.has_kernel:
            logger.debug("KernelParser")
            kernel_parser = KernelParser()
            kernel_parser.parse_events(self.events)
            self.kernel_stat = kernel_parser.kernel_stat

    def communication_parse(self):
        for comm_node in self.comm_node_list:
            if comm_node.step_name not in self.step_comm_stats:
                self.step_comm_stats[comm_node.step_name] = [0, 0]
            self.step_comm_stats[comm_node.step_name][0] += comm_node.total_time
            self.step_comm_stats[comm_node.step_name][1] += comm_node.real_time
            if comm_node.name not in self.total_comm_stats:
                self.total_comm_stats[comm_node.name] = [0, 0, 0, 0]
            self.total_comm_stats[comm_node.name][0] += 1
            bytes_one_value = 0
            for i in range(len(comm_node.input_shape)):
                if comm_node.input_type[i] == 'long int':
                    bytes_one_value = 8
                elif comm_node.input_type[i] == 'float':
                    bytes_one_value = 4
                elif comm_node.input_type[i] == 'int':
                    bytes_one_value = 4
                else:
                    logger.warning("Found an unknown tensor type: {}".format(comm_node.input_type[i]))
                    bytes_one_value = 0
                total_size = 1
                for size in comm_node.input_shape[i]:
                    total_size *= size
                self.total_comm_stats[comm_node.name][1] += total_size * bytes_one_value
            self.total_comm_stats[comm_node.name][2] += comm_node.total_time
            self.total_comm_stats[comm_node.name][3] += comm_node.real_time

    def analyze(self):
        self.recommendations = []
        dataloader_ratio = self.avg_costs.costs[ProfileRole.DataLoader] / self.avg_costs.costs[ProfileRole.Total]
        if dataloader_ratio > 0.05:
            text = "This run has high time cost on input data loading. " \
                   "{}% of the step time is in DataLoader. You could " \
                   "try to set num_workers on DataLoader's construction " \
                   "and enable multi-processes on data loading. " \
                   "Reference: <a href =\"{}\" target=\"_blank\">Single- and Multi-process Data Loading</a>".format(
                       round(dataloader_ratio * 100, 1),
                       "https://pytorch.org/docs/stable/data.html#single-and-multi-process-data-loading"
                   )
            self.recommendations.append(text)
