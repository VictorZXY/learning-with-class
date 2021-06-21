import os

import torch
import dgl
from ogb.utils.features import atom_to_feature_vector, bond_to_feature_vector, get_atom_feature_dims, \
    get_bond_feature_dims
from rdkit import Chem
from rdkit.Chem.rdmolops import GetAdjacencyMatrix
from torch.utils.data import Dataset
import numpy as np
import pandas as pd
from tqdm import tqdm
import torch.nn.functional as F
from scipy.constants import physical_constants

from commons.spherical_encoding import dist_emb

hartree2eV = physical_constants['hartree-electron volt relationship'][0]


class QM9Dataset(Dataset):
    """The QM9 Dataset. It loads the specified types of data into memory. The processed data is saved in eV units.

    The dataset can return these types of data and you choose them via the return_types parameter

    - class 0 : mol_graph
    - class 1 : raw_features: [n_atoms, 10]  10 features for each atom of the molecule (1 hot encoded atomic number, hybridization type, aromatic ...)
    - class 2 : coordinates: [n_atoms, 3] 3D coordinates of each atom
    - class 3 : mol_id: single number, id of molecule
    - class 4 : targets: tensor of shape [n_tasks] with the tasks specified by the target_tasks parameter
    - class 5 : one_hot_bond_types: [n_edges, 4] one hot encoding of bond type single, double, triple, aromatic
    - class 6 : edge_indices: [2, n_edges] list of edges
    - class 7 : smiles: SMILES representation of the molecule
    - class 8 : atomic_number_long: [n_atoms] atomic numbers

    The targets are:
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | Target | Property                         | Description                                                                       | Unit                                        |
    +========+==================================+===================================================================================+=============================================+
    | 0      | :math:`\mu`                      | Dipole moment                                                                     | :math:`\textrm{D}`                          |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 1      | :math:`\alpha`                   | Isotropic polarizability                                                          | :math:`{a_0}^3`                             |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 2      | :math:`\epsilon_{\textrm{HOMO}}` | Highest occupied molecular orbital energy                                         | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 3      | :math:`\epsilon_{\textrm{LUMO}}` | Lowest unoccupied molecular orbital energy                                        | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 4      | :math:`\Delta \epsilon`          | Gap between :math:`\epsilon_{\textrm{HOMO}}` and :math:`\epsilon_{\textrm{LUMO}}` | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 5      | :math:`\langle R^2 \rangle`      | Electronic spatial extent                                                         | :math:`{a_0}^2`                             |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 6      | :math:`\textrm{ZPVE}`            | Zero point vibrational energy                                                     | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 7      | :math:`U_0`                      | Internal energy at 0K                                                             | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 8      | :math:`U`                        | Internal energy at 298.15K                                                        | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 9      | :math:`H`                        | Enthalpy at 298.15K                                                               | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 10     | :math:`G`                        | Free energy at 298.15K                                                            | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 11     | :math:`c_{\textrm{v}}`           | Heat capavity at 298.15K                                                          | :math:`\frac{\textrm{cal}}{\textrm{mol K}}` |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+

    not predicted by dimenet, spherical message passing, E(n) equivariant graph neural networks:
    +--------+----------------------------------+-----------------------------------------------------------------------------------+
    | 12     | :math:`U_0^{\textrm{ATOM}}`      | Atomization energy at 0K                                                          | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 13     | :math:`U^{\textrm{ATOM}}`        | Atomization energy at 298.15K                                                     | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 14     | :math:`H^{\textrm{ATOM}}`        | Atomization enthalpy at 298.15K                                                   | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 15     | :math:`G^{\textrm{ATOM}}`        | Atomization free energy at 298.15K                                                | :math:`\textrm{eV}`                         |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 16     | :math:`A`                        | Rotational constant                                                               | :math:`\textrm{GHz}`                        |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 17     | :math:`B`                        | Rotational constant                                                               | :math:`\textrm{GHz}`                        |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+
    | 18     | :math:`C`                        | Rotational constant                                                               | :math:`\textrm{GHz}`                        |
    +--------+----------------------------------+-----------------------------------------------------------------------------------+---------------------------------------------+

    Parameters
    ----------
    return_types: list
        A list with which types of data should be loaded and returened by getitems. Possible options are
        ['mol_graph', 'complete_graph', 'raw_features', 'coordinates', 'mol_id', 'targets', 'one_hot_bond_types', 'edge_indices', 'smiles', 'atomic_number_long']
        and the default is ['mol_graph', 'targets']
    features: list
       A list specifying which features should be included in the returned graphs or raw features
       options are ['atom_one_hot', 'atomic_number_long', 'hybridizations', 'is_aromatic', 'constant_ones']
       and default is all except constant ones
    target_tasks: list
        A list specifying which targets should be included in the returend targets, if targets are returned.
        The targets are returned in eV units and saved as eV units in the processed data.
        options are ['A', 'B', 'C', 'mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv', 'u0_atom', 'u298_atom', 'h298_atom', 'g298_atom']
        and default is ['mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv']
        which is the stuff that is commonly predicted by papers like DimeNet, Equivariant GNNs, Spherical message passing
        The returned targets will be in the order specified by this list
    normalize: bool
        Whether or not the target (if they should be returned) are normalized to 0 mean and std 1
    prefetch_graphs: bool
        Whether or not to load the dgl graphs into memory. This takes a bit more memory and the upfront computation but
        the graph creation does not have to be done during training which is nice because it takes a long time and can
        slow down training

    Attributes
    ----------
    return_types: list
        A list with which types of data should be loaded and returened by getitems. Possible options are
        ['mol_graph', 'raw_features', 'coordinates', 'mol_id', 'targets', 'one_hot_bond_types', 'edge_indices', 'smiles', 'atomic_number_long']
        and the default is ['mol_graph', 'targets']
    target_tasks: list
        A list specifying which targets should be included in the returend targets, if targets are returned
        options are ['A', 'B', 'C', 'mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv', 'u0_atom', 'u298_atom', 'h298_atom', 'g298_atom']
        and default is ['mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv']
        which is the stuff that is commonly predicted by papers like DimeNet, Equivariant GNNs, Spherical message passing
        The returned targets will be in the order specified by this list
    features:
        possible features are ['standard_normal_noise', 'implicit-valence','degree','hybridization','chirality','mass','electronegativity','aromatic-bond','formal-charge','radical-electron','in-ring','atomic-number', 'vec1', 'vec2', 'vec3', 'vec-1', 'vec-2', 'vec-3', 'inv_vec1', 'inv_vec2', 'inv_vec3', 'inv_vec-1', 'inv_vec-2', 'inv_vec-3']

    features3d:
        possible features are ['standard_normal_noise', 'implicit-valence','degree','hybridization','chirality','mass','electronegativity','aromatic-bond','formal-charge','radical-electron','in-ring','atomic-number', 'vec1', 'vec2', 'vec3', 'vec-1', 'vec-2', 'vec-3', 'inv_vec1', 'inv_vec2', 'inv_vec3', 'inv_vec-1', 'inv_vec-2', 'inv_vec-3']
    e_features:
        possible are ['bond-type-onehot','stereo','conjugated','in-ring-edges']
    others: list
        TODO

    Examples
    --------
    >>> dataset = QM9Dataset(return_types=['mol_graph', 'targets', 'coordinates'])

    The dataset instance is an iterable

    >>> len(data)
    130831
    >>> g, label, coordinates = data[0]
    >>> g
    Graph(num_nodes=5, num_edges=8,
      ndata_schemes={'features': Scheme(shape=(10,), dtype=torch.float32)}
      edata_schemes={})
    """

    def __init__(self, return_types: list = None,
                 target_tasks: list = None,
                 normalize: bool = True, device='cuda:0', dist_embedding: bool = False, num_radial: int = 6,
                 prefetch_graphs=True, transform=None, **kwargs):
        self.return_type_options = ['mol_graph', 'complete_graph', 'mol_graph3d', 'complete_graph3d', 'san_graph',
                                    'mol_complete_graph',
                                    'se3Transformer_graph', 'se3Transformer_graph3d',
                                    'pairwise_distances', 'pairwise_distances_squared',
                                    'pairwise_indices',
                                    'raw_features', 'coordinates',
                                    'dist_embedding',
                                    'mol_id', 'targets',
                                    'one_hot_bond_types', 'edge_indices', 'smiles', 'atomic_number_long']
        self.qm9_directory = 'dataset/QM9'
        self.processed_file = 'qm9_processed.pt'
        self.distances_file = 'qm9_distances.pt'
        self.raw_qm9_file = 'qm9.csv'
        self.raw_spatial_data = 'qm9_eV.npz'
        self.atom_types = {'H': 0, 'C': 1, 'N': 2, 'O': 3, 'F': 4}
        self.symbols = {'H': 1, 'C': 6, 'N': 7, 'O': 8, 'F': 9}
        self.normalize = normalize
        self.device = device
        self.transform = transform

        self.num_radial = num_radial
        # data in the csv file is in Hartree units.
        self.unit_conversion = {'A': 1.0,
                                'B': 1.0,
                                'C': 1.0,
                                'mu': 1.0,
                                'alpha': 1.0,
                                'homo': hartree2eV,
                                'lumo': hartree2eV,
                                'gap': hartree2eV,
                                'r2': 1.0,
                                'zpve': hartree2eV,
                                'u0': hartree2eV,
                                'u298': hartree2eV,
                                'h298': hartree2eV,
                                'g298': hartree2eV,
                                'cv': 1.0,
                                'u0_atom': hartree2eV,
                                'u298_atom': hartree2eV,
                                'h298_atom': hartree2eV,
                                'g298_atom': hartree2eV}

        if return_types == None:  # set default
            self.return_types: list = ['mol_graph', 'targets']
        else:
            self.return_types: list = return_types
        for return_type in self.return_types:
            if not return_type in self.return_type_options: raise Exception(f'return_type not supported: {return_type}')

        if target_tasks == None or target_tasks == []:  # set default
            self.target_tasks = ['mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv']
        else:
            self.target_tasks: list = target_tasks
        for target_task in self.target_tasks:
            assert target_task in self.unit_conversion.keys()

        # load the data and get normalization values
        if not os.path.exists(os.path.join(self.qm9_directory, 'processed', self.processed_file)):
            self.process()
        data_dict = torch.load(os.path.join(self.qm9_directory, 'processed', self.processed_file))

        self.features_tensor = data_dict['atom_features']

        self.e_features_tensor = data_dict['edge_features']
        self.coordinates = data_dict['coordinates']
        self.edge_indices = data_dict['edge_indices']

        self.meta_dict = {k: data_dict[k] for k in ('mol_id', 'edge_slices', 'atom_slices', 'n_atoms')}

        self.atom_padding_indices = torch.tensor(get_atom_feature_dims(), dtype=torch.long, device=device)[None, :]
        self.bond_padding_indices = torch.tensor(get_atom_feature_dims(), dtype=torch.long, device=device)[None, :]

        if 'san_graph' in self.return_types:
            self.eig_vals = data_dict['eig_vals']
            self.eig_vecs = data_dict['eig_vecs']

        if 'smiles' in self.return_types:
            self.smiles = pd.read_csv(os.path.join(self.qm9_directory, self.raw_qm9_file))['smiles']

        self.prefetch_graphs = prefetch_graphs
        if self.prefetch_graphs and any(return_type in self.return_types for return_type in
                                        ['mol_graph', 'mol_graph3d', 'se3Transformer_graph', 'se3Transformer_graph3d']):
            print(
                'Load molecular graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.mol_graphs = []
            for idx, n_atoms in tqdm(enumerate(self.meta_dict['n_atoms'])):
                e_start = self.meta_dict['edge_slices'][idx]
                e_end = self.meta_dict['edge_slices'][idx + 1]
                edge_indices = self.edge_indices[:, e_start: e_end]
                self.mol_graphs.append(dgl.graph((edge_indices[0], edge_indices[1]), num_nodes=n_atoms))
        self.pairwise = {}  # for memoization
        if self.prefetch_graphs and (
                'complete_graph' in self.return_types or 'complete_graph3d' in self.return_types or 'san_graph' in self.return_types):
            print(
                'Load complete graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.complete_graphs = []
            for idx, n_atoms in tqdm(enumerate(self.meta_dict['n_atoms'])):
                src, dst = self.get_pairwise(n_atoms)
                self.complete_graphs.append(dgl.graph((src, dst)))
        if self.prefetch_graphs and (
                'mol_complete_graph' in self.return_types or 'mol_complete_graph3d' in self.return_types):
            print(
                'Load mol_complete_graph graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.mol_complete_graphs = []
            for idx, n_atoms in tqdm(enumerate(self.meta_dict['n_atoms'])):
                src, dst = self.get_pairwise(n_atoms)
                self.mol_complete_graphs.append(
                    dgl.heterograph({('atom', 'bond', 'atom'): (src, dst), ('atom', 'complete', 'atom'): (src, dst)}))
        print('Finish loading data into memory')

        self.avg_degree = data_dict['avg_degree']
        # indices of the tasks that should be retrieved
        self.task_indices = torch.tensor([list(self.unit_conversion.keys()).index(task) for task in self.target_tasks])
        # select targets in the order specified by the target_tasks argument

        self.targets = data_dict['targets'].index_select(dim=1, index=self.task_indices)  # [130831, n_tasks]
        self.targets_mean = self.targets.mean(dim=0)
        self.targets_std = self.targets.std(dim=0)
        if self.normalize:
            self.targets = ((self.targets - self.targets_mean) / self.targets_std)
        self.targets_mean = self.targets_mean.to(device)
        self.targets_std = self.targets_std.to(device)
        # get a tensor that is 1000 for all targets that are energies and 1.0 for all other ones
        self.eV2meV = torch.tensor(
            [1.0 if list(self.unit_conversion.values())[task_index] == 1.0 else 1000 for task_index in
             self.task_indices]).to(self.device)  # [n_tasks]
        self.dist_embedder = dist_emb(num_radial=6).to(device)
        self.dist_embedding = dist_embedding

    def get_pairwise(self, n_atoms):
        if n_atoms in self.pairwise:
            return self.pairwise[n_atoms]
        else:
            src = torch.repeat_interleave(torch.arange(n_atoms), n_atoms - 1)
            dst = torch.cat([torch.cat([torch.arange(n_atoms)[:idx], torch.arange(n_atoms)[idx + 1:]]) for idx in
                             range(n_atoms)])  # without self loops
            self.pairwise[n_atoms] = (src, dst)
            return src, dst

    def __len__(self):
        return len(self.meta_dict['mol_id'])

    def __getitem__(self, idx):
        """

        Parameters
        ----------
        idx: integer between 0 and len(self) - 1

        Returns
        -------
        tuple of all data specified via the return_types parameter of the constructor
        """
        data = []
        e_start = self.meta_dict['edge_slices'][idx]
        e_end = self.meta_dict['edge_slices'][idx + 1]
        start = self.meta_dict['atom_slices'][idx]
        n_atoms = self.meta_dict['n_atoms'][idx]

        for return_type in self.return_types:
            data.append(self.data_by_type(idx, return_type, e_start, e_end, start, n_atoms))
        return tuple(data)

    def get_graph(self, idx, e_start, e_end, n_atoms):
        if self.prefetch_graphs:
            g = self.mol_graphs[idx]
        else:
            edge_indices = self.edge_indices[:, e_start: e_end]
            g = dgl.graph((edge_indices[0], edge_indices[1]), num_nodes=n_atoms)
        return g

    def get_complete_graph(self, idx, n_atoms):
        if self.prefetch_graphs:
            g = self.complete_graphs[idx]
        else:
            src, dst = self.get_pairwise(n_atoms)
            g = dgl.graph((src, dst))
        return g

    def get_mol_complete_graph(self, idx, e_start, e_end, n_atoms):
        if self.prefetch_graphs:
            g = self.mol_complete_graphs[idx]
        else:
            edge_indices = self.edge_indices[:, e_start: e_end]
            src, dst = self.get_pairwise(n_atoms)
            g = dgl.heterograph({('atom', 'bond', 'atom'): (edge_indices[0], edge_indices[1]),
                                 ('atom', 'complete', 'atom'): (src, dst)})
        return g

    def data_by_type(self, idx, return_type, e_start, e_end, start, n_atoms):
        if return_type == 'mol_graph':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['feat'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            g.edata['feat'] = self.e_features_tensor[e_start: e_end].to(self.device)
            return g
        elif return_type == 'mol_graph3d':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            return g
        elif return_type == 'complete_graph':  # complete graph without self loops
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['feat'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            g.edata['d'] = torch.norm(g.ndata['x'][g.edges()[0]] - g.ndata['x'][g.edges()[1]], p=2, dim=-1).unsqueeze(
                -1)

            # set edge features with padding for virtual edges
            bond_features = self.e_features_tensor[e_start: e_end].to(self.device)
            e_features = self.bond_padding_indices.clone().expand((n_atoms * n_atoms, -1))
            edge_indices = self.edge_indices[:, e_start: e_end]
            bond_indices = edge_indices[0] * n_atoms + edge_indices[1]
            e_features[bond_indices] = bond_features
            src, dst = self.get_pairwise(n_atoms)
            g.edata['feat'] = e_features[src * n_atoms + dst]
            if self.dist_embedding:
                g.edata['d_rbf'] = self.dist_embedder(g.edata['feat']).to(self.device)
            return g
        elif return_type == 'complete_graph3d':
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            g.edata['d'] = torch.norm(g.ndata['x'][g.edges()[0]] - g.ndata['x'][g.edges()[1]], p=2, dim=-1).unsqueeze(
                -1)
            if self.dist_embedding:
                g.edata['d_rbf'] = self.dist_embedder(g.edata['feat']).to(self.device)
            return g
        if return_type == 'mol_complete_graph':
            g = self.get_mol_complete_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['feat'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            if self.e_features_tensor != None:
                g.edges['bond'].data['feat'] = self.e_features_tensor[e_start: e_end].to(self.device)
            return g
        if return_type == 'san_graph':
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['feat'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            eig_vals = self.eig_vals[idx].to(self.device)
            sign_flip = torch.rand(eig_vals.shape[0], device=self.device)
            sign_flip[sign_flip >= 0.5] = 1.0
            sign_flip[sign_flip < 0.5] = -1.0
            eig_vecs = self.eig_vecs[start: start + n_atoms].to(self.device) * sign_flip.unsqueeze(0)
            eig_vals = eig_vals.unsqueeze(0).repeat(n_atoms, 1)
            g.ndata['pos_enc'] = torch.stack([eig_vals, eig_vecs], dim=-1)
            if self.e_features_tensor != None:
                e_features = self.e_features_tensor[e_start: e_end].to(self.device)
                g.edata['feat'] = torch.zeros(g.number_of_edges(), e_features.shape[1], dtype=torch.float32,
                                              device=self.device)
                g.edata['real'] = torch.zeros(g.number_of_edges(), dtype=torch.long, device=self.device)
                edge_indices = self.edge_indices[:, e_start: e_end].to(self.device)
                g.edges[edge_indices[0], edge_indices[1]].data['feat'] = e_features
                g.edges[edge_indices[0], edge_indices[1]].data['real'] = torch.ones(e_features.shape[0],
                                                                                    dtype=torch.long,
                                                                                    device=self.device)  # This indicates real edges
            return g
        elif return_type == 'se3Transformer_graph' or return_type == 'se3Transformer_graph3d':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            x = self.coordinates[start: start + n_atoms].to(self.device)
            if self.transform:
                x = self.transform(x)
            g.ndata['x'] = x
            edge_indices = self.edge_indices[:, e_start: e_end].to(self.device)
            g.edata['d'] = x[edge_indices[0]] - x[edge_indices[1]]
            if self.e_features_tensor != None and return_type == 'se3Transformer_graph':
                g.edata['feat'] = self.e_features_tensor[e_start: e_end].to(self.device)
            return g
        elif return_type == 'raw_features':
            return self.features_tensor[start: start + n_atoms]
        elif return_type == 'coordinates':
            return self.coordinates[start: start + n_atoms]
        elif return_type == 'mol_id':
            return self.meta_dict['mol_id'][idx]
        elif return_type == 'targets':
            return self.targets[idx]
        elif return_type == 'edge_indices':
            return self.meta_dict['edge_indices'][:, e_start: e_end]
        elif return_type == 'smiles':
            return self.smiles[self.meta_dict['mol_id'][idx]]
        else:
            raise Exception(f'return type not supported: ', return_type)

    def process(self):
        print('processing data from ({}) and saving it to ({})'.format(self.qm9_directory,
                                                                       os.path.join(self.qm9_directory, 'processed')))

        # load qm9 data with spatial coordinates
        data_qm9 = dict(np.load(os.path.join(self.qm9_directory, self.raw_spatial_data), allow_pickle=True))
        coordinates = torch.tensor(data_qm9['R'], dtype=torch.float)
        # Read the QM9 data with SMILES information
        molecules_df = pd.read_csv(os.path.join(self.qm9_directory, self.raw_qm9_file))

        atom_slices = [0]
        edge_slices = [0]
        total_eigvecs = []
        total_eigvals = []
        all_atom_features = []
        all_edge_features = []
        edge_indices = []  # edges of each molecule in coo format
        targets = []  # the 19 properties that should be predicted for the QM9 dataset
        total_atoms = 0
        total_edges = 0
        avg_degree = 0  # average degree in the dataset
        # go through all molecules in the npz file
        for mol_idx, n_atoms in tqdm(enumerate(data_qm9['N'])):
            # get the molecule using the smiles representation from the csv file
            mol = Chem.MolFromSmiles(molecules_df['smiles'][data_qm9['id'][mol_idx]])
            # add hydrogen bonds to molecule because they are not in the smiles representation
            mol = Chem.AddHs(mol)

            atom_features_list = []
            for atom in mol.GetAtoms():
                atom_features_list.append(atom_to_feature_vector(atom))
            all_atom_features.append(torch.tensor(atom_features_list, dtype=torch.long))

            adj = GetAdjacencyMatrix(mol, useBO=False, force=True)
            max_freqs = 10
            adj = torch.tensor(adj).float()
            D = torch.diag(adj.sum(dim=0))
            L = D - adj
            N = adj.sum(dim=0) ** -0.5
            L_sym = torch.eye(n_atoms) - N * L * N
            eig_vals, eig_vecs = torch.symeig(L_sym, eigenvectors=True)
            idx = eig_vals.argsort()[0: max_freqs]  # Keep up to the maximum desired number of frequencies
            eig_vals, eig_vecs = eig_vals[idx], eig_vecs[:, idx]

            # Sort, normalize and pad EigenVectors
            eig_vecs = eig_vecs[:, eig_vals.argsort()]  # increasing order
            eig_vecs = F.normalize(eig_vecs, p=2, dim=1, eps=1e-12, out=None)
            if n_atoms < max_freqs:
                eig_vecs = F.pad(eig_vecs, (0, max_freqs - n_atoms), value=float('nan'))
                eig_vals = F.pad(eig_vals, (0, max_freqs - n_atoms), value=float('nan'))

            total_eigvecs.append(eig_vecs)
            total_eigvals.append(eig_vals.unsqueeze(0))

            edges_list = []
            edge_features_list = []
            for bond in mol.GetBonds():
                i = bond.GetBeginAtomIdx()
                j = bond.GetEndAtomIdx()
                edge_feature = bond_to_feature_vector(bond)

                # add edges in both directions
                edges_list.append((i, j))
                edge_features_list.append(edge_feature)
                edges_list.append((j, i))
                edge_features_list.append(edge_feature)
            # Graph connectivity in COO format with shape [2, num_edges]
            edge_index = torch.tensor(edges_list, dtype=torch.long).T
            edge_features = torch.tensor(edge_features_list, dtype=torch.long)

            avg_degree += (len(edges_list) / 2) / n_atoms

            # get all 19 attributes that should be predicted, so we drop the first two entries (name and smiles)
            target = torch.tensor(molecules_df.iloc[data_qm9['id'][mol_idx]][2:], dtype=torch.float)
            targets.append(target)
            edge_indices.append(edge_index)
            all_edge_features.append(edge_features)

            total_edges += len(edges_list)
            total_atoms += n_atoms
            edge_slices.append(total_edges)
            atom_slices.append(total_atoms)

        # convert targets to eV units
        targets = torch.stack(targets) * torch.tensor(list(self.unit_conversion.values()))[None, :]
        data_dict = {'mol_id': data_qm9['id'],
                     'n_atoms': torch.tensor(data_qm9['N'], dtype=torch.long),
                     'atom_slices': torch.tensor(atom_slices, dtype=torch.long),
                     'edge_slices': torch.tensor(edge_slices, dtype=torch.long),
                     'eig_vecs': torch.cat(total_eigvecs).float(),
                     'eig_vals': torch.cat(total_eigvals).float(),
                     'edge_indices': torch.cat(edge_indices, dim=1),
                     'atom_features': torch.cat(all_atom_features, dim=0),
                     'edge_features': torch.cat(all_edge_features, dim=0),
                     'atomic_number_long': torch.tensor(data_qm9['Z'], dtype=torch.long)[:, None],
                     'coordinates': coordinates,
                     'targets': targets,
                     'avg_degree': avg_degree / len(data_qm9['id'])
                     }

        if not os.path.exists(os.path.join(self.qm9_directory, 'processed')):
            os.mkdir(os.path.join(self.qm9_directory, 'processed'))
        torch.save(data_dict, os.path.join(self.qm9_directory, 'processed', self.processed_file))
