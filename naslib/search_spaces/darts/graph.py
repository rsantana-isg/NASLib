import torch
import yaml
import numpy as np
from scipy.special import softmax
from torch import nn

from naslib.search_spaces.core import EdgeOpGraph, NodeOpGraph
from naslib.search_spaces.core.primitives import FactorizedReduce, ReLUConvBN, Stem, Identity


class Cell(EdgeOpGraph):
    def __init__(self, primitives, cell_type, C_prev_prev, C_prev, C, reduction_prev, ops_dict, *args, **kwargs):
        self.primitives = primitives
        self.cell_type = cell_type
        self.C_prev_prev = C_prev_prev
        self.C_prev = C_prev
        self.C = C
        self.reduction_prev = reduction_prev
        self.ops_dict = ops_dict
        super(Cell, self).__init__(*args, **kwargs)

    def _build_graph(self):
        # Input Nodes: Previous / Previous-Previous cell
        preprocessing0 = FactorizedReduce(self.C_prev_prev, self.C, affine=False) \
            if self.reduction_prev else ReLUConvBN(self.C_prev_prev, self.C, 1, 1, 0, affine=False)
        preprocessing1 = ReLUConvBN(self.C_prev, self.C, 1, 1, 0, affine=False)

        self.add_node(0, type='input', preprocessing=preprocessing0, desc='previous-previous')
        self.add_node(1, type='input', preprocessing=preprocessing1, desc='previous')

        # 4 intermediate nodes
        self.add_node(2, type='inter', comb_op='sum')
        self.add_node(3, type='inter', comb_op='sum')
        self.add_node(4, type='inter', comb_op='sum')
        self.add_node(5, type='inter', comb_op='sum')

        # Output node
        self.add_node(6, type='output', comb_op='cat_channels')

        # Edges: input-inter and inter-inter
        for to_node in self.inter_nodes():
            for from_node in range(to_node):
                stride = 2 if self.cell_type == 'reduction' and from_node < 2 else 1
                self.add_edge(
                    from_node, to_node, op=None, op_choices=self.primitives,
                    op_kwargs={'C': self.C, 'stride': stride, 'out_node_op': 'sum', 'ops_dict': self.ops_dict,
                               'affine': False},
                    to_node=to_node, from_node=from_node)

        # Edges: inter-output
        self.add_edge(2, 6, op=Identity())
        self.add_edge(3, 6, op=Identity())
        self.add_edge(4, 6, op=Identity())
        self.add_edge(5, 6, op=Identity())

    @classmethod
    def from_config(cls, graph_dict, primitives, cell_type, C_prev_prev,
                    C_prev, C, reduction_prev, *args, **kwargs):
        graph = cls(primitives, cell_type, C_prev_prev, C_prev, C,
                    reduction_prev, *args, **kwargs)

        graph.clear()
        # Input Nodes: Previous / Previous-Previous cell
        for node, attr in graph_dict['nodes'].items():
            if 'preprocessing' in attr:
                if attr['preprocessing'] == 'FactorizedReduce':
                    input_args = {'C_in': graph.C_prev_prev, 'C_out': graph.C,
                                  'affine': False}
                else:
                    input_args = {'C_in': graph.C_prev_prev, 'C_out': graph.C,
                                  'kernel_size': 1, 'stride': 1, 'padding': 0,
                                  'affine': False}

                preprocessing = eval(attr['preprocessing'])(**input_args)

                graph.add_node(node, type=attr['type'],
                               preprocessing=preprocessing)
            else:
                graph.add_nodes_from([(node, attr)])

        for edge, attr in graph_dict['edges'].items():
            from_node, to_node = eval(edge)
            graph.add_edge(*eval(edge), **{k: eval(v) for k, v in attr.items() if k
                                           != 'op'})
            graph[from_node][to_node]['op'] = None if attr['op'] != 'Identity' else eval(attr['op'])()

        return graph


class MacroGraph(NodeOpGraph):
    def __init__(self, config, primitives, ops_dict, *args, **kwargs):
        self.config = config
        self.primitives = primitives
        self.ops_dict = ops_dict
        super(MacroGraph, self).__init__(*args, **kwargs)

    def _build_graph(self):
        num_layers = self.config['layers']
        C = self.config['init_channels']
        C_curr = self.config['stem_multiplier'] * C

        stem = Stem(C_curr=C_curr)
        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C

        # TODO: set the input edges to the first cell in a nicer way
        self.add_node(0, type='input')
        self.add_node(1, op=stem, type='stem')
        self.add_node('1b', op=stem, type='stem')

        # Normal and reduction cells
        reduction_prev = False
        for cell_num in range(num_layers):
            if cell_num in [num_layers // 3, 2 * num_layers // 3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False

            self.add_node(cell_num + 2,
                          op=Cell(primitives=self.primitives, C_prev_prev=C_prev_prev, C_prev=C_prev, C=C_curr,
                                  reduction_prev=reduction_prev, cell_type='reduction' if reduction else 'normal',
                                  ops_dict=self.ops_dict),
                          type='reduction' if reduction else 'normal')
            reduction_prev = reduction
            C_prev_prev, C_prev = C_prev, self.config['channel_multiplier'] * C_curr

        pooling = nn.AdaptiveAvgPool2d(1)
        classifier = nn.Linear(C_prev, self.config['num_classes'])

        self.add_node(num_layers + 2, op=pooling,
                      transform=lambda x: x[0], type='pooling')
        self.add_node(num_layers + 3, op=classifier,
                      transform=lambda x: x[0].view(x[0].size(0), -1), type='output')

        # Edges
        self.add_edge(0, 1)
        self.add_edge(0, '1b')

        # Parallel edge that's why MultiDiGraph
        self.add_edge(1, 2, type='input', desc='previous-previous')
        self.add_edge('1b', 2, type='input', desc='previous')

        for i in range(3, num_layers + 2):
            self.add_edge(i - 2, i, type='input', desc='previous-previous')
            self.add_edge(i - 1, i, type='input', desc='previous')

        # From output of normal-reduction cell to pooling layer
        self.add_edge(num_layers + 1, num_layers + 2)
        self.add_edge(num_layers + 2, num_layers + 3)


    #TODO: merge with sample method
    def discretize(self, n_ops_per_edge=1, n_input_edges=None):
        """
        n_ops_per_edge:
            1; number of sampled operations per edge in cell
        n_input_edges:
            None; list equal with length with number of intermediate
        nodes. Determines the number of predecesor nodes for each of them
        """
        # create a new graph that we will discretize
        new_graph = MacroGraph(self.config, self.primitives)

        for node in new_graph:
            _cell = self.get_node_op(node)
            cell = new_graph.get_node_op(node)
            if not isinstance(cell, Cell):
                continue

            for edge in _cell.edges:
                if bool(set(_cell.output_nodes()) & set(edge)):
                    continue
                op_choices = _cell.get_edge_op_choices(*edge)
                alphas = _cell.get_edge_arch_weights(*edge).detach().numpy()
                sampled_op = np.array(op_choices)[np.argsort(alphas)[-n_ops_per_edge:]]
                cell[edge[0]][edge[1]]['op_choices'] = [*sampled_op]

            if n_input_edges is not None:
                for inter_node, k in zip(_cell.inter_nodes(), n_input_edges):
                    # in case the start node index is not 0
                    node_idx = list(_cell.nodes).index(inter_node)
                    prev_node_choices = list(_cell.nodes)[:node_idx]
                    assert k <= len(prev_node_choices), 'cannot sample more'
                    ' than number of predecesor nodes'

                    previous_argmax_alphas = [
                        max(softmax(_cell.get_edge_arch_weights(
                            i, inter_node
                        ).detach().numpy())) for i in prev_node_choices
                    ]
                    sampled_input_edges = np.array(
                        prev_node_choices
                    )[np.argsort(previous_argmax_alphas)[-k:]]

                    for i in set(prev_node_choices) - set(sampled_input_edges):
                        cell.remove_edge(i, inter_node)

        return new_graph


    def sample(self, same_cell_struct=True, n_ops_per_edge=1,
               n_input_edges=None, dist=None, seed=1):
        """
        same_cell_struct:
            True; if the sampled cell topology is the same or not
        n_ops_per_edge:
            1; number of sampled operations per edge in cell
        n_input_edges:
            None; list equal with length with number of intermediate
        nodes. Determines the number of predecesor nodes for each of them
        dist:
            None; distribution to sample operations in edges from
        seed:
            1; random seed
        """
        # create a new graph that we will discretize
        new_graph = MacroGraph(self.config, self.primitives)
        np.random.seed(seed)
        seeds = {'normal': seed+1, 'reduction': seed+2}

        for node in new_graph:
            cell = new_graph.get_node_op(node)
            if not isinstance(cell, Cell):
                continue

            if same_cell_struct:
                np.random.seed(seeds[new_graph.get_node_type(node)])

            for edge in cell.edges:
                if bool(set(cell.output_nodes()) & set(edge)):
                    continue
                op_choices = cell.get_edge_op_choices(*edge)
                sampled_op = np.random.choice(op_choices, n_ops_per_edge,
                                              False, p=dist)
                cell[edge[0]][edge[1]]['op_choices'] = [*sampled_op]

            if n_input_edges is not None:
                for inter_node, k in zip(cell.inter_nodes(), n_input_edges):
                    # in case the start node index is not 0
                    node_idx = list(cell.nodes).index(inter_node)
                    prev_node_choices = list(cell.nodes)[:node_idx]
                    assert k <= len(prev_node_choices), 'cannot sample more'
                    ' than number of predecesor nodes'

                    sampled_input_edges = np.random.choice(prev_node_choices,
                                                           k, False)
                    for i in set(prev_node_choices) - set(sampled_input_edges):
                        cell.remove_edge(i, inter_node)

        return new_graph


    @classmethod
    def from_config(cls, config=None, filename=None):
        with open(filename, 'r') as f:
            graph_dict = yaml.safe_load(f)

        if config is None:
            raise ('No configuration provided')

        graph = cls(config, [])

        graph_type = graph_dict['type']
        edges = [(*eval(e), attr) for e, attr in graph_dict['edges'].items()]
        graph.clear()
        graph.add_edges_from(edges)

        C = config['init_channels']
        C_curr = config['stem_multiplier'] * C

        stem = Stem(C_curr=C_curr)
        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C

        for node, attr in graph_dict['nodes'].items():
            node_type = attr['type']
            if node_type == 'input':
                graph.add_node(node, type='input')
            elif node_type == 'stem':
                graph.add_node(node, op=stem, type='stem')
            elif node_type in ['normal', 'reduction']:
                assert attr['op']['type'] == 'Cell'
                graph.add_node(node,
                               op=Cell.from_config(attr['op'], primitives=attr['op']['primitives'],
                                                   C_prev_prev=C_prev_prev, C_prev=C_prev,
                                                   C=C_curr,
                                                   reduction_prev=graph_dict['nodes'][node - 1]['type'] == 'reduction',
                                                   cell_type=node_type),
                               type=node_type)
                C_prev_prev, C_prev = C_prev, config['channel_multiplier'] * C_curr
            elif node_type == 'pooling':
                pooling = nn.AdaptiveAvgPool2d(1)
                graph.add_node(node, op=pooling, transform=lambda x: x[0],
                               type='pooling')
            elif node_type == 'output':
                classifier = nn.Linear(C_prev, config['num_classes'])
                graph.add_node(node, op=classifier, transform=lambda x:
                x[0].view(x[0].size(0), -1), type='output')

        return graph
