""" Operations """
import torch
import torch.nn as nn
from collections import namedtuple
from pytorch_cls.core.config import cfg
import pytorch_cls.core.net as net

Genotype = namedtuple('Genotype', 'normal normal_concat reduce reduce_concat')

PRIMITIVES = [
    'max_pool_3x3',
    'avg_pool_3x3',
    'skip_connect',  # identity
    'sep_conv_3x3',
    'sep_conv_5x5',
    'dil_conv_3x3',
    'dil_conv_5x5',
    'none'
]

OPS = {
    'none': lambda C, stride, affine: Zero(stride),
    'avg_pool_3x3': lambda C, stride, affine: PoolBN('avg', C, 3, stride, 1, affine=affine),
    'max_pool_3x3': lambda C, stride, affine: PoolBN('max', C, 3, stride, 1, affine=affine),
    'skip_connect': lambda C, stride, affine:
    Identity() if stride == 1 else FactorizedReduce(C, C, affine=affine),
    'sep_conv_3x3': lambda C, stride, affine: SepConv(C, C, 3, stride, 1, affine=affine),
    'sep_conv_5x5': lambda C, stride, affine: SepConv(C, C, 5, stride, 2, affine=affine),
    'sep_conv_7x7': lambda C, stride, affine: SepConv(C, C, 7, stride, 3, affine=affine),
    # 5x5
    'dil_conv_3x3': lambda C, stride, affine: DilConv(C, C, 3, stride, 2, 2, affine=affine),
    # 9x9
    'dil_conv_5x5': lambda C, stride, affine: DilConv(C, C, 5, stride, 4, 2, affine=affine),
    'conv_7x1_1x7': lambda C, stride, affine: FacConv(C, C, 7, stride, 3, affine=affine)
}


def to_dag(C_in, gene, reduction):
    """ generate discrete ops from gene """
    dag = nn.ModuleList()
    for edges in gene:
        row = nn.ModuleList()
        for op_name, s_idx in edges:
            # reduction cell & from input nodes => stride = 2
            stride = 2 if reduction and s_idx < 2 else 1
            op = OPS[op_name](C_in, stride, True)
            if not isinstance(op, Identity):  # Identity does not use drop path
                op = nn.Sequential(
                    op,
                    DropPath_()
                )
            op.s_idx = s_idx
            row.append(op)
        dag.append(row)

    return dag


def drop_path_(x, drop_prob, training):
    if training and drop_prob > 0.:
        keep_prob = 1. - drop_prob
        # per data point mask; assuming x in cuda.
        mask = torch.cuda.FloatTensor(x.size(0), 1, 1, 1).bernoulli_(keep_prob)
        x.div_(keep_prob).mul_(mask)

    return x


class DropPath_(nn.Module):
    def __init__(self, p=0.):
        """ [!] DropPath is inplace module
        Args:
            p: probability of an path to be zeroed.
        """
        super().__init__()
        self.p = p

    def extra_repr(self):
        return 'p={}, inplace'.format(self.p)

    def forward(self, x):
        drop_path_(x, self.p, self.training)

        return x


class PoolBN(nn.Module):
    """
    AvgPool or MaxPool - BN
    """

    def __init__(self, pool_type, C, kernel_size, stride, padding, affine=True):
        """
        Args:
            pool_type: 'max' or 'avg'
        """
        super().__init__()
        if pool_type.lower() == 'max':
            self.pool = nn.MaxPool2d(kernel_size, stride, padding)
        elif pool_type.lower() == 'avg':
            self.pool = nn.AvgPool2d(
                kernel_size, stride, padding, count_include_pad=False)
        else:
            raise ValueError()

        self.bn = nn.BatchNorm2d(C, affine=affine)

    def forward(self, x):
        out = self.pool(x)
        out = self.bn(out)
        return out

    @staticmethod
    def complexity(cx, C, kernel_size, stride, padding):
        cx = net.complexity_maxpool2d(cx, kernel_size, stride, padding)
        cx = net.complexity_batchnorm2d(cx, C)
        return cx


class StdConv(nn.Module):
    """ Standard conv
    ReLU - Conv - BN
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_out, kernel_size, stride, padding, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)

    @staticmethod
    def complexity(cx, C_in, C_out, kernel_size, stride, padding):
        cx = net.complexity_conv2d(
            cx, C_in, C_out, kernel_size, stride, padding, bias=False)
        cx = net.complexity_batchnorm2d(cx, C_out)
        return cx


class FacConv(nn.Module):
    """ Factorized conv
    ReLU - Conv(Kx1) - Conv(1xK) - BN
    """

    def __init__(self, C_in, C_out, kernel_length, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_in, (kernel_length, 1),
                      stride=(stride, 1), padding=(padding, 0), bias=False),
            nn.Conv2d(C_in, C_out, (1, kernel_length),
                      stride=(1, stride), padding=(0, padding), bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)

    @staticmethod
    def complexity(cx, C_in, C_out, kernel_length, stride, padding):
        cx = net.complexity_conv2d(
            cx, C_in, C_out, (kernel_length, 1), (stride, 1), (padding, 0), bias=False)
        cx = net.complexity_conv2d(
            cx, C_in, C_out, (1, kernel_length), (1, stride), (0, padding), bias=False)
        cx = net.complexity_batchnorm2d(cx, C_out)
        return cx


class DilConv(nn.Module):
    """ (Dilated) depthwise separable conv
    ReLU - (Dilated) depthwise separable - Pointwise - BN

    If dilation == 2, 3x3 conv => 5x5 receptive field
                      5x5 conv => 9x9 receptive field
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, dilation, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_in, kernel_size, stride, padding, dilation=dilation, groups=C_in,
                      bias=False),
            nn.Conv2d(C_in, C_out, 1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)

    @staticmethod
    def complexity(cx, C_in, C_out, kernel_size, stride, padding):
        cx = net.complexity_conv2d(
            cx, C_in, C_in, kernel_size, stride, padding, groups=C_in, bias=False)
        cx = net.complexity_conv2d(cx, C_in, C_out, 1, 1, 0, bias=False)
        cx = net.complexity_batchnorm2d(cx, C_out)
        return cx


class SepConv(nn.Module):
    """ Depthwise separable conv
    DilConv(dilation=1) * 2
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            DilConv(C_in, C_in, kernel_size, stride,
                    padding, dilation=1, affine=affine),
            DilConv(C_in, C_out, kernel_size, 1,
                    padding, dilation=1, affine=affine)
        )

    def forward(self, x):
        return self.net(x)

    @staticmethod
    def complexity(cx, C_in, C_out, kernel_size, stride, padding):
        cx = DilConv.complexity(cx, C_in, C_in, kernel_size, stride, padding)
        cx = DilConv.complexity(cx, C_in, C_out, kernel_size, 1, padding)
        return cx


class Identity(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x

    @staticmethod
    def complexity(cx):
        return cx


class Zero(nn.Module):
    def __init__(self, stride):
        super().__init__()
        self.stride = stride

    def forward(self, x):
        if self.stride == 1:
            return x * 0.

        # re-sizing by stride
        return x[:, :, ::self.stride, ::self.stride] * 0.

    @staticmethod
    def complexity(cx, stride):
        h, w, flops, params, acts = cx["h"], cx["w"], cx["flops"], cx["params"], cx["acts"]
        stride_h = stride if stride is int else stride[0]
        stride_w = stride if stride is int else stride[1]
        h = h // stride_h
        w = w // stride_w
        return {"h": h, "w": w, "flops": flops, "params": params, "acts": acts}


class FactorizedReduce(nn.Module):
    """
    Reduce feature map size by factorized pointwise(stride=2).
    """

    def __init__(self, C_in, C_out, affine=True):
        super().__init__()
        self.relu = nn.ReLU()
        self.conv1 = nn.Conv2d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.conv2 = nn.Conv2d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.bn = nn.BatchNorm2d(C_out, affine=affine)

    def forward(self, x):
        x = self.relu(x)
        out = torch.cat([self.conv1(x), self.conv2(x[:, :, 1:, 1:])], dim=1)
        out = self.bn(out)
        return out

    @staticmethod
    def complexity(cx, C_in, C_out):
        cx = net.complexity_conv2d(cx, C_in, C_out // 2, 1, 2, 0, bias=False)
        cx = net.complexity_conv2d(cx, C_in, C_out // 2, 1, 2, 0, bias=False)
        cx = net.complexity_batchnorm2d(cx, C_out)
        return cx


class AugmentCell(nn.Module):
    """ Cell for augmentation
    Each edge is discrete.
    """

    def __init__(self, genotype, C_pp, C_p, C, reduction_p, reduction):
        super().__init__()
        self.reduction = reduction
        self.n_nodes = len(genotype.normal)

        if reduction_p:
            self.preproc0 = FactorizedReduce(C_pp, C)
        else:
            self.preproc0 = StdConv(C_pp, C, 1, 1, 0)
        self.preproc1 = StdConv(C_p, C, 1, 1, 0)

        # generate dag
        if reduction:
            gene = genotype.reduce
            self.concat = genotype.reduce_concat
        else:
            gene = genotype.normal
            self.concat = genotype.normal_concat

        self.dag = to_dag(C, gene, reduction)

    def forward(self, s0, s1):
        s0 = self.preproc0(s0)
        s1 = self.preproc1(s1)

        states = [s0, s1]
        for edges in self.dag:
            s_cur = sum(op(states[op.s_idx]) for op in edges)
            states.append(s_cur)

        s_out = torch.cat([states[i] for i in self.concat], dim=1)

        return s_out


class AuxiliaryHead(nn.Module):
    """ Auxiliary head in 2/3 place of network to let the gradient flow well """

    def __init__(self, input_size, C, n_classes):
        """ assuming input size 7x7 or 8x8 """
        assert input_size in [7, 8]
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.AvgPool2d(5, stride=input_size-5, padding=0,
                         count_include_pad=False),  # 2x2 out
            nn.Conv2d(C, 128, kernel_size=1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 768, kernel_size=2, bias=False),  # 1x1 out
            nn.BatchNorm2d(768),
            nn.ReLU(inplace=True)
        )
        self.linear = nn.Linear(768, n_classes)

    def forward(self, x):
        out = self.net(x)
        out = out.view(out.size(0), -1)  # flatten
        logits = self.linear(out)
        return logits


class AugmentCNN(nn.Module):
    """ Augmented CNN model """

    def __init__(self, input_size, C_in, C, n_classes, n_layers, auxiliary, genotype,
                 stem_multiplier=3):
        """
        Args:
            input_size: size of height and width (assuming height = width)
            C_in: # of input channels
            C: # of starting model channels
        """
        super().__init__()
        self.C_in = C_in
        self.C = C
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.genotype = genotype
        # aux head position
        self.aux_pos = 2*n_layers//3 if auxiliary else -1

        C_cur = stem_multiplier * C
        self.stem = nn.Sequential(
            nn.Conv2d(C_in, C_cur, 3, 1, 1, bias=False),
            nn.BatchNorm2d(C_cur)
        )

        C_pp, C_p, C_cur = C_cur, C_cur, C

        self.cells = nn.ModuleList()
        reduction_p = False
        for i in range(n_layers):
            if i in [n_layers//3, 2*n_layers//3]:
                C_cur *= 2
                reduction = True
            else:
                reduction = False

            cell = AugmentCell(genotype, C_pp, C_p, C_cur,
                               reduction_p, reduction)
            reduction_p = reduction
            self.cells.append(cell)
            C_cur_out = C_cur * len(cell.concat)
            C_pp, C_p = C_p, C_cur_out

            if i == self.aux_pos:
                # [!] this auxiliary head is ignored in computing parameter size
                #     by the name 'aux_head'
                self.aux_head = AuxiliaryHead(input_size//4, C_p, n_classes)

        self.gap = nn.AdaptiveAvgPool2d(1)
        if cfg.DARTS.DROPOUT_RATIO > 0.0:
            self.dropout = nn.Dropout(p=cfg.DARTS.DROPOUT_RATIO)
        self.linear = nn.Linear(C_p, n_classes)

    def forward(self, x):
        s0 = s1 = self.stem(x)

        aux_logits = None
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1)
            if i == self.aux_pos and self.training:
                aux_logits = self.aux_head(s1)

        out = self.gap(s1)
        out = out.view(out.size(0), -1)  # flatten
        x = self.dropout(x) if hasattr(self, "dropout") else x
        logits = self.linear(out)
        if self.aux_pos == -1:
            return logits
        else:
            return logits, aux_logits

    def drop_path_prob(self, p):
        """ Set drop path probability """
        for module in self.modules():
            if isinstance(module, DropPath_):
                module.p = p


class AugmentCNN_ImageNet(nn.Module):
    """ Augmented CNN model """

    def __init__(self, input_size, C_in, C, n_classes, n_layers, auxiliary, genotype,
                 stem_multiplier=3):
        """
        Args:
            input_size: size of height and width (assuming height = width)
            C_in: # of input channels
            C: # of starting model channels
        """
        super().__init__()
        self.C_in = C_in
        self.C = C
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.genotype = genotype
        # aux head position
        self.aux_pos = 2*n_layers//3 if auxiliary else -1

        self.stem0 = nn.Sequential(
            nn.Conv2d(C_in, C // 2, kernel_size=3,
                      stride=2, padding=1, bias=False),
            nn.BatchNorm2d(C // 2),
            nn.ReLU(inplace=True),
            nn.Conv2d(C // 2, C, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(C),
        )
        self.stem1 = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.Conv2d(C, C, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(C),
        )

        C_prev_prev, C_prev, C_curr = C, C, C

        self.cells = nn.ModuleList()
        reduction_prev = True
        for i in range(n_layers):
            if i in [n_layers//3, 2*n_layers//3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False

            cell = AugmentCell(genotype, C_prev_prev, C_prev,
                               C_curr, reduction_prev, reduction)
            reduction_prev = reduction
            self.cells.append(cell)
            C_prev_prev, C_prev = C_prev, len(cell.concat) * C_curr

            if i == self.aux_pos:
                # [!] this auxiliary head is ignored in computing parameter size
                #     by the name 'aux_head'
                self.aux_head = AuxiliaryHead(input_size//4, C_prev, n_classes)

        self.gap = nn.AdaptiveAvgPool2d(1)
        # dropout
        if cfg.DARTS.DROPOUT_RATIO > 0.0:
            self.dropout = nn.Dropout(p=cfg.DARTS.DROPOUT_RATIO)
        self.linear = nn.Linear(C_prev, n_classes)

    def forward(self, x):
        s0 = self.stem0(x)
        s1 = self.stem1(s0)
        aux_logits = None
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1)
            if i == self.aux_pos and self.training:
                aux_logits = self.aux_head(s1)

        out = self.gap(s1)
        out = out.view(out.size(0), -1)  # flatten
        out = self.dropout(out) if hasattr(self, "dropout") else out
        logits = self.linear(out)
        if self.aux_pos == -1:
            return logits
        else:
            return logits, aux_logits

    def drop_path_prob(self, p):
        """ Set drop path probability """
        for module in self.modules():
            if isinstance(module, DropPath_):
                module.p = p


def NASNet():
    NASNet = Genotype(
        normal=[
            [('sep_conv_5x5', 1),
             ('sep_conv_3x3', 0)],
            [('sep_conv_5x5', 0),
             ('sep_conv_3x3', 0)],
            [('avg_pool_3x3', 1),
             ('skip_connect', 0)],
            [('avg_pool_3x3', 0),
             ('avg_pool_3x3', 0)],
            [('sep_conv_3x3', 1),
             ('skip_connect', 1)],
        ],
        normal_concat=[2, 3, 4, 5, 6],
        reduce=[
            [('sep_conv_5x5', 1),
             ('sep_conv_7x7', 0)],
            [('max_pool_3x3', 1),
             ('sep_conv_7x7', 0)],
            [('avg_pool_3x3', 1),
             ('sep_conv_5x5', 0)],
            [('skip_connect', 3),
             ('avg_pool_3x3', 2)],
            [('sep_conv_3x3', 2),
             ('max_pool_3x3', 1)],
        ],
        reduce_concat=[4, 5, 6],
    )
    return NASNet


def AmoebaNet():
    AmoebaNet = Genotype(
        normal=[
            [('avg_pool_3x3', 0),
             ('max_pool_3x3', 1)],
            [('sep_conv_3x3', 0),
             ('sep_conv_5x5', 2)],
            [('sep_conv_3x3', 0),
             ('avg_pool_3x3', 3)],
            [('sep_conv_3x3', 1),
             ('skip_connect', 1)],
            [('skip_connect', 0),
             ('avg_pool_3x3', 1)],
        ],
        normal_concat=[4, 5, 6],
        reduce=[
            [('avg_pool_3x3', 0),
             ('sep_conv_3x3', 1)],
            [('max_pool_3x3', 0),
             ('sep_conv_7x7', 2)],
            [('sep_conv_7x7', 0),
             ('avg_pool_3x3', 1)],
            [('max_pool_3x3', 0),
             ('max_pool_3x3', 1)],
            [('conv_7x1_1x7', 0),
             ('sep_conv_3x3', 5)],
        ],
        reduce_concat=[3, 4, 6]
    )
    return AmoebaNet


def DARTS_V1():
    DARTS_V1 = Genotype(normal=[[('sep_conv_3x3', 1), ('sep_conv_3x3', 0)], [('skip_connect', 0), ('sep_conv_3x3', 1)], [('skip_connect', 0), ('sep_conv_3x3', 1)], [('sep_conv_3x3', 0), ('skip_connect', 2)]], normal_concat=[
                        2, 3, 4, 5], reduce=[[('max_pool_3x3', 0), ('max_pool_3x3', 1)], [('skip_connect', 2), ('max_pool_3x3', 0)], [('max_pool_3x3', 0), ('skip_connect', 2)], [('skip_connect', 2), ('avg_pool_3x3', 0)]], reduce_concat=[2, 3, 4, 5])
    return DARTS_V1


def DARTS_V2():
    DARTS_V2 = Genotype(normal=[[('sep_conv_3x3', 0), ('sep_conv_3x3', 1)], [('sep_conv_3x3', 0), ('sep_conv_3x3', 1)], [('sep_conv_3x3', 1), ('skip_connect', 0)], [('skip_connect', 0), ('dil_conv_3x3', 2)]], normal_concat=[
                        2, 3, 4, 5], reduce=[[('max_pool_3x3', 0), ('max_pool_3x3', 1)], [('skip_connect', 2), ('max_pool_3x3', 1)], [('max_pool_3x3', 0), ('skip_connect', 2)], [('skip_connect', 2), ('max_pool_3x3', 1)]], reduce_concat=[2, 3, 4, 5])
    return DARTS_V2


def darts_cnn():
    if len(cfg.DARTS.GENOTYPE) > 10:
        genotype = eval(cfg.DARTS.GENOTYPE)
    elif cfg.DARTS.NAME == 'darts_v1':
        genotype = DARTS_V1()
    elif cfg.DARTS.NAME == 'darts_v2':
        genotype = DARTS_V2()
    elif cfg.DARTS.NAME == 'nasnet':
        genotype = NASNet()
    elif cfg.DARTS.NAME == 'amoebanet':
        genotype = AmoebaNet()
    else:
        raise NotImplementedError
    build_fun = AugmentCNN if cfg.DARTS.MODEL_TYPE == 'cifar10' else AugmentCNN_ImageNet

    aux_ = True if cfg.DARTS.AUX_WEIGHT > 0 else False

    net = build_fun(cfg.TRAIN.IM_SIZE, 3, cfg.DARTS.INIT_CHANNEL, cfg.MODEL.NUM_CLASSES, cfg.DARTS.LAYERS, aux_, genotype)
    return net


def test():
    net = DARTS_V2()
    print('test darts v2')
    x = torch.randn(2, 3, 32, 32)
    y = net(x)
    print(y[0].shape)
    print(y[1].shape)
    print('pass!')

    net = DARTS_V1()
    print('test darts v1')
    x = torch.randn(2, 3, 32, 32)
    y = net(x)
    print(y[0].shape)
    print(y[1].shape)
    print('pass!')

    net = NASNet()
    print('test nasnet')
    x = torch.randn(2, 3, 32, 32)
    y = net(x)
    print(y[0].shape)
    print(y[1].shape)
    print('pass!')

    net = AmoebaNet()
    print('test amoebaNet')
    x = torch.randn(2, 3, 32, 32)
    y = net(x)
    print(y[0].shape)
    print(y[1].shape)
    print('pass!')
