#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function

import unittest
import torch
import torch.nn as nn
import torch.nn.functional as F


class AttentionTest(unittest.TestCase):
    
    def test_multihead_attention(self):
        from nmtlab.modules.multihead_attention import MultiHeadAttention
        attention = MultiHeadAttention(hidden_size=16, additive=True)
        enc_states = torch.randn((4, 8, 16))
        dec_states = torch.randn((4, 10, 16))
        attention(dec_states, enc_states, enc_states)
