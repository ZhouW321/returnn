#! /usr/bin/python2.7

import numpy
import theano
import theano.tensor as T
from Util import hdf5_dimension
from math import sqrt
from CTC import CTCOp
from BestPathDecoder import BestPathDecodeOp

"""
        META LAYER
"""
class Container(object):
  def __init__(self, layer_class, name = ""):
    self.params = {}
    self.attrs = {}
    self.layer_class = layer_class
    self.name = name
  
  @staticmethod
  def initialize():
    Layer.rng = numpy.random.RandomState(1234)
    
  def save(self, head):
    grp = head.create_group(self.name)
    grp.attrs['class'] = self.layer_class
    for p in self.params.keys():
      value = self.params[p].get_value()
      dset = grp.create_dataset(p, value.shape, dtype='f')
      dset[...] = value
    for p in self.attrs.keys():
      grp.attrs[p] = self.attrs[p]

  def load(self, head):
    grp = head[self.name]
    assert grp.attrs['class'] == self.layer_class, "invalid layer class (expected " + self.layer_class + " got " + grp.attrs['class'] + ")"
    for p in grp:
      assert self.params[p].get_value().shape == grp[p].shape, "invalid layer parameter shape (expected  " + str(self.params[p].get_value().shape) + " got " + str(grp[p].shape) + ")"
      self.params[p].set_value(grp[p][...])
    for p in grp.attrs.keys():
      self.attrs[p] = grp.attrs[p]
        
  def num_params(self):
    return sum([numpy.prod(self.params[p].get_value().shape[0:]) for p in self.params.keys()])
  
  def get_params(self):
    return { p : self.params[p].get_value() for p in self.params.keys() }
  
  def set_params(self, params):
    for p in params.keys():
      self.params[p].set_value(params[p])
      
  def add_param(self, param, name = ""):
    if name == "": name = "param_%d" % len(self.params)
    self.params[name] = param
    return param

  def set_attr(self, name, value):
    self.attrs[name] = value
      
  def create_bias(self, n):
    return theano.shared(value = numpy.zeros((n,), dtype=theano.config.floatX), borrow=True, name = 'b_' + self.name)
  
  def create_random_weights(self, n, m, s):
    values = numpy.asarray(self.rng.normal(loc = 0.0, scale = s, size=(n, m)), dtype=theano.config.floatX)
    return theano.shared(value = values, borrow = True, name = 'W_' + self.name)
  
  def create_uniform_weights(self, n, m, p = 0):
    if p == 0: p = n + m
    #values = numpy.asarray(self.rng.uniform(low = - 1 / sqrt(p), high = 1 / sqrt(p), size=(n, m)), dtype=theano.config.floatX)
    values = numpy.asarray(self.rng.uniform(low = - sqrt(6) / sqrt(p), high = sqrt(6) / sqrt(p), size=(n, m)), dtype=theano.config.floatX)
    return theano.shared(value = values, borrow = True, name = 'W_' + self.name)
  
  def create_forward_weights(self, n, m):
    n_in = n + m
    scale = numpy.sqrt(12. / (n_in))
    return self.create_random_weights(n, m, scale)
  
class Layer(Container):
  def __init__(self, n_in, n_out, layer_class, mask = "unity", dropout = 0, name = ""):
    super(Layer, self).__init__(layer_class, name = name)
    self.n_in = n_in
    self.n_out = n_out
    self.set_attr('mask', mask)
    self.set_attr('dropout', dropout)
    self.set_attr('n_in', n_in)
    self.set_attr('n_out', n_out)
    self.b = self.add_param(self.create_bias(n_out), 'b')
    self.mass = T.constant(1.)
    if mask == "unity":
      self.mask = T.constant(1.)
      if dropout > 0:
        self.mass = T.constant(dropout)
    elif mask == "dropout":
      srng = theano.tensor.shared_randomstreams.RandomStreams(self.rng.randint(1234))
      self.mask = T.cast(srng.binomial(n=1, p=1-dropout, size=(n_in, n_out)), theano.config.floatX)
      
"""
        OUTPUT LAYERS
"""

class OutputLayer(Layer):
  def __init__(self, sources, index, n_out, loss = 'ce', dropout = 0, mask = "unity", layer_class = "softmax", name = ""):
    super(OutputLayer, self).__init__(sum([s.n_out for s in sources]), n_out, layer_class, mask, dropout, name = name)
    self.z = self.b
    self.W_in = [ self.add_param(self.create_forward_weights(source.n_out, n_out), "W") for source in sources ]
    for source, W in zip(sources, self.W_in): self.z += T.dot(source.output, self.mass * self.mask * W)
    self.index = index
    self.i = self.index.flatten() #T.cast(T.reshape(index, (self.z.shape[0] * self.z.shape[1],)), 'int8')
    self.loss = loss
    if self.loss == 'priori': self.priori = theano.shared(value = numpy.ones((n_out,), dtype=theano.config.floatX), borrow=True)
    self.initialize()
    
  def initialize(self):
    self.y_m = T.reshape(self.z, (self.z.shape[0] * self.z.shape[1], self.z.shape[2]), ndim = 2)
    #self.y_m = T.dimshuffle(T.flatten(T.dimshuffle(self.z, (2, 0, 1)), ndim = 2), (1, 0))
    #self.y_m = self.z.dimshuffle(2, 0, 1).flatten(ndim = 2).dimshuffle(1, 0)
    #self.y_m = T.reshape(self.z, (self.z.shape[0] * self.z.shape[1], self.z.shape[2]), ndim = 2)
    if self.loss == 'ce': self.p_y_given_x = T.nnet.softmax(self.y_m)
    elif self.loss == 'sse': self.p_y_given_x = self.y_m
    elif self.loss == 'ctc': self.p_y_given_x = T.reshape(T.nnet.softmax(self.y_m), self.z.shape)
    elif self.loss == 'priori': self.p_y_given_x = T.nnet.softmax(self.y_m) / self.priori
    else: assert False, "invalid loss: " + self.loss
    self.y_pred = T.argmax(self.p_y_given_x, axis = -1)

  def cost(self, y):
    y_f = T.cast(T.reshape(y, (y.shape[0] * y.shape[1]), ndim = 1), 'int32')
    #y_f = y.dimshuffle(2, 0, 1).flatten(ndim = 2).dimshuffle(1, 0)
    #y_f = y.flatten()
    known_grads = None
    if self.loss == 'ce' or self.loss == 'priori':
      pcx = self.p_y_given_x[(self.i > 0).nonzero(), y_f[(self.i > 0).nonzero()]] 
      return -T.sum(T.log(pcx)), known_grads
    elif self.loss == 'sse':
      y_oh = T.eq(T.shape_padleft(T.arange(self.n_out), y_f.ndim), T.shape_padright(y_f, 1))
      return T.mean(T.sqr(self.p_y_given_x[(self.i > 0).nonzero()] - y_oh[(self.i > 0).nonzero()])), known_grads
    elif self.loss == 'ctc':
      err, grad = CTCOp()(self.p_y_given_x, y, T.sum(self.index, axis = 0))
      known_grads = {self.z: grad}
      return err.sum(), known_grads
    else: assert False

    #* T.sum(self.p_y_given_x[(self.i > 0).nonzero()] * T.log(self.p_y_given_x[(self.i > 0).nonzero()]))
  def entropy(self):
    return -T.sum(self.p_y_given_x[(self.i > 0).nonzero()] * T.log(self.p_y_given_x[(self.i > 0).nonzero()]))

  def errors(self, y):
    if self.loss == 'ctc': return T.sum(BestPathDecodeOp()(self.p_y_given_x, y, T.sum(self.index, axis = 0)))
    y_f = y.flatten() #T.cast(T.reshape(y, (y.shape[0] * y.shape[1]), ndim = 1), 'int32') #y_f = y.flatten(ndim=1)
    if y_f.dtype.startswith('int'):
      return T.sum(T.neq(self.y_pred[(self.i > 0).nonzero()], y_f[(self.i > 0).nonzero()]))
    else: raise NotImplementedError()
       
"""
        HIDDEN LAYERS
"""

class HiddenLayer(Layer):
  def __init__(self, source, n_in, n_out, activation, dropout = 0, mask = "unity", layer_class = "hidden", name = ""):
    super(HiddenLayer, self).__init__(n_in, n_out, layer_class, mask, dropout, name = name)
    self.n_in = n_in
    self.n_out = n_out
    self.activation = activation
    self.W_in = self.add_param(self.create_forward_weights(n_in, n_out), 'W_in')
    self.source = source
    self.set_attr('n_in', n_in)
    
class ForwardLayer(HiddenLayer):
  def __init__(self, source, n_in, n_out, activation=T.tanh, dropout = 0, mask = "unity", layer_class = "hidden", name = ""):
    super(ForwardLayer, self).__init__(source, n_in, n_out, activation, dropout, mask, layer_class = layer_class, name = name)
    self.W_in.set_value(self.create_uniform_weights(n_in, n_out).get_value())
    z = T.dot(source, self.mass * self.mask * self.W_in) + self.b
    self.output = (z if self.activation is None else self.activation(z))
    
class RecurrentLayer(HiddenLayer):
  def __init__(self, source, index, n_in, n_out, activation = T.tanh, reverse = False, truncation = -1, compile = True, dropout = 0, mask = "unity", layer_class = "recurrent", name = ""):
    super(RecurrentLayer, self).__init__(source, n_in, n_out, activation, dropout, mask, layer_class = layer_class, name = name)
    self.act = self.create_bias(n_out)
    W_in, self.W_re = self.create_recurrent_weights(n_in, n_out)
    self.W_in.set_value(W_in.get_value())
    self.add_param(self.W_re, 'W_re')
    self.index = T.cast(index, theano.config.floatX)
    self.o = theano.shared(value = numpy.ones((n_out,), dtype=theano.config.floatX), borrow=True)
    self.reverse = reverse
    self.truncation = truncation
    self.set_attr('recurrent', True)
    self.set_attr('reverse', reverse)
    self.set_attr('truncation', truncation)
    if compile: self.compile()
  
  def compile(self):
    def step(x_t, i_t, h_p):
      i = T.outer(i_t, self.o)
      z = T.dot(x_t, self.mass * self.mask * self.W_in) + T.dot(h_p, self.W_re) + self.b
      #z = (T.dot(x_t, self.mass * self.mask * self.W_in) + self.b) * T.nnet.sigmoid(T.dot(h_p, self.W_re))
      h_t = (z if self.activation is None else self.activation(z))
      return h_t * i    
    self.output, _ = theano.scan(step,
                                 go_backwards = self.reverse,
                                 truncate_gradient = self.truncation,
                                 sequences = [self.source, self.index],
                                 outputs_info = [T.alloc(self.act, self.source.shape[1], self.n_out)])
    self.output = self.output[::-(2 * self.reverse - 1)]
    
  def create_recurrent_weights(self, n, m):
    nin = n + m + m + m
    return self.create_random_weights(n, m, nin), self.create_random_weights(m, m, nin)
   
class LstmLayer(RecurrentLayer):
  def __init__(self, source, index, n_in, n_out, activation = T.nnet.sigmoid, reverse = False, truncation = -1, sharpgates = 'none' , dropout = 0, mask = "unity", layer_class = "lstm", name = ""):
    super(LstmLayer, self).__init__(source, index, n_in, n_out * 4, activation, reverse, truncation, False, dropout, mask, layer_class = layer_class, name = name)
    if not isinstance(activation, (list, tuple)):
      activation = [T.tanh, T.nnet.sigmoid, T.nnet.sigmoid, T.nnet.sigmoid, T.tanh]
    else: assert len(activation) == 5, "lstm activations have to be specified as 5 tuple (input, ingate, forgetgate, outgate, output)"
    self.set_attr('sharpgates', sharpgates)
    self.set_attr('n_out', n_out)
    CI, GI, GF, GO, CO = activation #T.tanh, T.nnet.sigmoid, T.nnet.sigmoid, T.nnet.sigmoid, T.tanh
    self.act = self.create_bias(n_out)
    self.state = self.create_bias(n_out)
    W_in, W_re = self.create_lstm_weights(n_in, n_out)
    if CI == T.nnet.sigmoid or CO == T.nnet.sigmoid:
      self.W_in.set_value(W_in.get_value()) # * 0.5) # * 0.000001)
      self.W_re.set_value(W_re.get_value()) # * 0.5) # * 0.000001)
    else:  
      self.W_in.set_value(W_in.get_value())
      self.W_re.set_value(W_re.get_value())
    self.o.set_value(numpy.ones((n_out,), dtype=theano.config.floatX))    
    self.n_out /= 4
    if sharpgates == 'global': self.sharpness = self.create_uniform_weights(3, n_out)
    elif sharpgates == 'shared':
      if not hasattr(LstmLayer, 'sharpgates'):
        LstmLayer.sharpgates = self.create_bias(3)
        self.add_param(LstmLayer.sharpgates, 'gate_scaling')
      self.sharpness = LstmLayer.sharpgates
    elif sharpgates == 'single':
      if not hasattr(LstmLayer, 'sharpgates'):
        LstmLayer.sharpgates = self.create_bias(1)
        self.add_param(LstmLayer.sharpgates, 'gate_scaling')
      self.sharpness = LstmLayer.sharpgates
    else: self.sharpness = theano.shared(value = numpy.zeros((3,), dtype=theano.config.floatX), borrow=True, name = 'lambda') #self.create_bias(3)
    self.sharpness.set_value(numpy.ones(self.sharpness.get_value().shape, dtype = theano.config.floatX))
    if sharpgates != 'none' and sharpgates != "shared" and sharpgates != "single": self.add_param(self.sharpness, 'gate_scaling')
    
    def step(x_t, i_t, s_p, h_p, mask):
      i = T.outer(i_t, self.o)
      z = T.dot(x_t, self.mass * mask * self.W_in) + T.dot(h_p, self.W_re) + self.b
      partition = z.shape[1] / 4
      input = CI(z[:,:partition])
      ingate = GI(self.sharpness[0] * z[:,partition: 2 * partition])
      forgetgate = GF(self.sharpness[1] * z[:,2 * partition:3 * partition])
      s_t = input * ingate + s_p * forgetgate
      outgate = GO(self.sharpness[2] * z[:,3 * partition:4 * partition])
      h_t = CO(s_t) * outgate
      return s_t * i, h_t * i
    
    [state, self.output], _ = theano.scan(step,
                                          truncate_gradient = self.truncation,
                                          go_backwards = self.reverse,
                                          sequences = [self.source, self.index],
                                          non_sequences = [self.mask],
                                          outputs_info = [ T.alloc(self.state, self.source.shape[1], n_out),
                                                           T.alloc(self.act, self.source.shape[1], n_out), ])
    self.output = self.output[::-(2 * self.reverse - 1)]
  
  def create_lstm_weights(self, n, m):
    n_in = n + 4 * m + m + 4 * m
    #scale = numpy.sqrt(12. / (n_in))
    #return self.create_random_weights(n, m * 4, scale), self.create_random_weights(m, m * 4, scale)
    #return self.create_uniform_weights(n, m * 4, n + m), self.create_uniform_weights(m, m * 4, n + m)
    return self.create_uniform_weights(n, m * 4, n + m + m * 4), self.create_uniform_weights(m, m * 4, n + m + m * 4)
  
  
class GateLstmLayer(RecurrentLayer):
  def __init__(self, source, index, n_in, n_out, activation = T.nnet.sigmoid, reverse = False, truncation = -1, sharpgates = 'none' , dropout = 0, mask = "unity", name = "lstm"):
    super(GateLstmLayer, self).__init__(source, index, n_in, n_out * 4, activation, reverse, truncation, False, dropout, mask, name = name)
    if not isinstance(activation, (list, tuple)):
      activation = [T.tanh, T.nnet.sigmoid, T.nnet.sigmoid, T.nnet.sigmoid, T.tanh]
    else: assert len(activation) == 5, "lstm activations have to be specified as 5 tuple (input, ingate, forgetgate, outgate, output)"
    CI, GI, GF, GO, CO = activation #T.tanh, T.nnet.sigmoid, T.nnet.sigmoid, T.nnet.sigmoid, T.tanh
    self.act = self.create_bias(n_out)
    self.state = self.create_bias(n_out)
    W_in, W_re = self.create_lstm_weights(n_in, n_out)
    if CI == T.nnet.sigmoid or CO == T.nnet.sigmoid:
      self.W_in.set_value(W_in.get_value()) # * 0.5) # * 0.000001)
      self.W_re.set_value(W_re.get_value()) # * 0.5) # * 0.000001)
    else:  
      self.W_in.set_value(W_in.get_value())
      self.W_re.set_value(W_re.get_value())
    self.o.set_value(numpy.ones((n_out,), dtype=theano.config.floatX))    
    self.n_out /= 4
    if sharpgates == 'global': self.sharpness = self.create_uniform_weights(3, n_out)
    elif sharpgates == 'shared':
      if not hasattr(LstmLayer, 'sharpgates'):
        LstmLayer.sharpgates = self.create_bias(3)
        self.add_param(LstmLayer.sharpgates)
      self.sharpness = LstmLayer.sharpgates
    elif sharpgates == 'single':
      if not hasattr(LstmLayer, 'sharpgates'):
        LstmLayer.sharpgates = self.create_bias(1)
        self.add_param(LstmLayer.sharpgates)
      self.sharpness = LstmLayer.sharpgates
    else: self.sharpness = self.create_bias(3)
    self.sharpness.set_value(numpy.ones(self.sharpness.get_value().shape, dtype = theano.config.floatX))
    if sharpgates != 'none' and sharpgates != "shared" and sharpgates != "single": self.add_param(self.sharpness)
    
    self.ingate = self.create_bias(n_out)
    self.forgetgate = self.create_bias(n_out)
    self.outgate = self.create_bias(n_out)
    
    def step(x_t, i_t, s_p, h_p, ig_p, fg_p, og_p, mask):
      i = T.outer(i_t, self.o)
      z = T.dot(x_t, self.mass * mask * self.W_in) + T.dot(h_p, self.W_re) + self.b
      partition = z.shape[1] / 4
      input = CI(z[:,:partition])
      ingate = GI(self.sharpness[0] * z[:,partition: 2 * partition])
      forgetgate = GF(self.sharpness[1] * z[:,2 * partition:3 * partition])
      s_t = input * ingate + s_p * forgetgate
      outgate = GO(self.sharpness[2] * z[:,3 * partition:4 * partition])
      h_t = CO(s_t) * outgate
      return s_t * i, h_t * i, ingate * i, forgetgate * i, outgate * i
    
    [state, self.output, self.input_gate, self.forget_gate, self.output_gate], _ = theano.scan(step,
                                                                                   truncate_gradient = self.truncation,
                                                                                   go_backwards = self.reverse,
                                                                                   sequences = [self.source, self.index],
                                                                                   non_sequences = [self.mask],
                                                                                   outputs_info = [ T.alloc(self.state, self.source.shape[1], n_out),
                                                                                                    T.alloc(self.act, self.source.shape[1], n_out),
                                                                                                    T.alloc(self.ingate, self.source.shape[1], n_out),
                                                                                                    T.alloc(self.forgetgate, self.source.shape[1], n_out),
                                                                                                    T.alloc(self.outgate, self.source.shape[1], n_out) ])
    self.output = self.output[::-(2 * self.reverse - 1)]
    self.input_gate = self.input_gate[::-(2 * self.reverse - 1)]
    self.forget_gate = self.forget_gate[::-(2 * self.reverse - 1)]
    self.output_gate = self.output_gate[::-(2 * self.reverse - 1)]
  
  def create_lstm_weights(self, n, m):
    n_in = n + 4 * m + m + 4 * m
    #scale = numpy.sqrt(12. / (n_in))
    #return self.create_random_weights(n, m * 4, scale), self.create_random_weights(m, m * 4, scale)
    #return self.create_uniform_weights(n, m * 4, n + m), self.create_uniform_weights(m, m * 4, n + m)
    return self.create_uniform_weights(n, m * 4, n + m + m * 4), self.create_uniform_weights(m, m * 4, n + m + m * 4)

class LstmPeepholeLayer(LstmLayer):
  def __init__(self, source, index, n_in, n_out, activation = T.nnet.sigmoid, reverse = False, truncation = -1, dropout = 0, mask = "unity", name = "lstm"):
    super(LstmPeepholeLayer, self).__init__(source, index, n_in, n_out, activation, reverse, truncation, dropout, mask, name = name)
    self.peeps_in = self.create_peeps(n_out)
    self.peeps_forget = self.create_peeps(n_out)
    self.peeps_out = self.create_peeps(n_out)
    self.add_param(self.peeps_in)
    self.add_param(self.peeps_forget)
    self.add_param(self.peeps_out)
    
    def peep(x_t, i_t, s_p, h_p, mask):
      i = T.outer(i_t, self.o)
      z = T.dot(x_t, self.mass * mask * self.W_in) + T.dot(h_p, self.W_re) + self.b
      partition = z.shape[1] / 4
      CI = T.tanh
      CO = T.tanh
      G = T.nnet.sigmoid
      pi = s_p * self.peeps_in
      pf = s_p * self.peeps_forget
      input = CI(z[:,:partition])
      ingate = G(z[:,partition: 2 * partition] + pi)
      forgetgate = G(z[:,2 * partition:3 * partition] + pf)
      s_t = input * ingate + s_p * forgetgate
      po = s_t * self.peeps_out
      outgate = G(z[:,3 * partition:4 * partition] + po)
      h_t = CO(s_t) * outgate
      return s_t * i, h_t * i
    
    [pstate, peep_output], _ = theano.scan(peep,
                                       truncate_gradient = 0,
                                       go_backwards = self.reverse,
                                       sequences=[self.source, self.index],
                                       non_sequences=[self.mask],
                                       outputs_info=[ T.alloc(self.state, self.source.shape[1], n_out),
                                                      T.alloc(self.act, self.source.shape[1], n_out), ])
    self.output = 0.5 * (peep_output + self.output)
    self.output = self.output[::-(2 * self.reverse - 1)]
    
  def create_peeps(self, n):
    values = numpy.asarray(self.rng.normal(loc=0.0,
                                           scale=numpy.sqrt(.6/(4 * self.n_out)),
                                           size=(n, )), dtype=theano.config.floatX)
    return theano.shared(value=values, borrow=True)

"""
        NETWORKS
"""
        
class LayerNetwork(object):
  def __init__(self, n_in, n_out, mask = "unity"):
    self.x = T.tensor3('x')
    self.y = T.imatrix('y')
    self.c = T.imatrix('c')
    self.i = T.bmatrix('i')
    Layer.initialize()
    self.hidden_info = []
    self.n_in = n_in
    self.n_out = n_out
    self.mask = mask
  
  @classmethod
  def from_config(cls, config, mask = "unity"):
    num_inputs = hdf5_dimension(config.list('train')[0], 'inputPattSize') * config.int('window', 1)
    loss = config.value('loss', 'ce')
    num_outputs = hdf5_dimension(config.list('train')[0], 'numLabels')
    if loss == 'ctc':
      num_outputs += 1 #add blank
    hidden_size = config.int_list('hidden_size')
    assert len(hidden_size) > 0, "no hidden layers specified"
    hidden_type = config.list('hidden_type')
    assert len(hidden_type) <= len(hidden_size), "too many hidden layer types"
    hidden_name = config.list('hidden_name')
    assert len(hidden_name) <= len(hidden_size), "too many hidden layer names"
    if len(hidden_type) != len(hidden_size):
      n_hidden_type = len(hidden_type) 
      for i in xrange(len(hidden_size) - len(hidden_type)):
        if n_hidden_type == 1:
          hidden_type.append(hidden_type[0])
        else:  
          hidden_type.append("forward")
    if len(hidden_name) != len(hidden_size):
      for i in xrange(len(hidden_size) - len(hidden_name)):
        hidden_name.append("_")
    for i, name in enumerate(hidden_name):
      if name == "_": hidden_name[i] = "hidden_%d" % i

    task = config.value('task', 'train')
    L1_reg = config.float('L1_reg', 0.0)
    L2_reg = config.float('L2_reg', 0.0)
    bidirectional = config.bool('bidirectional', True)
    truncation = config.int('truncation', -1)
    actfct = config.list('activation')
    dropout = config.list('dropout', [0.0])
    sharpgates = config.value('sharpgates', 'none')
    entropy = config.float('entropy', 0.0)
    if len(actfct) < len(hidden_size):
      for i in xrange(len(hidden_size) - len(actfct)):
        actfct.append("logistic")
    if len(dropout) < len(hidden_size) + 1:
      for i in xrange(len(hidden_size) + 1 - len(dropout)):
        dropout.append(0.0)
    dropout = [float(d) for d in dropout]
    network = cls(num_inputs, num_outputs, mask)
    activations = { 'logistic' : T.nnet.sigmoid,
                    'tanh' : T.tanh,
                    'relu': lambda z : (T.sgn(z) + 1) * z * 0.5,
                    'identity' : lambda z : z,
                    'one' : lambda z : 1,
                    'zero' : lambda z : 0,
                    'softsign': lambda z : z / (1.0 + abs(z)),
                    'softsquare': lambda z : 1 / (1.0 + z * z),
                    'maxout': lambda z : T.max(z, axis = 0),
                    'sin' : T.sin,
                    'cos' : T.cos }
    for i in xrange(len(hidden_size)):
      if ':' in actfct[i]:
        acts = []
        for a in actfct[i].split(':'):
          assert activations.has_key(a), "invalid activation function: " + a
          acts.append((a, activations[a]))
      else:
        assert activations.has_key(actfct[i]), "invalid activation function: " + actfct[i]
        acts = (actfct[i], activations[actfct[i]])
      network.add_hidden(hidden_name[i], hidden_size[i], hidden_type[i], acts)
    if task == 'pretrain':
      loss = 'layer'
      task = 'train'
      network.add_hidden("pretrain_layer",
                         config.int('pretrain_layer_size', hidden_size[-1]),
                         config.value('pretrain_layer_type', hidden_type[-1]),
                         config.value('pretrain_layer_activation', actfct[-1]))
    network.initialize(loss, L1_reg, L2_reg, dropout, bidirectional, truncation, sharpgates, entropy)
    return network

  @classmethod
  def from_model(cls, model, mask = None):
    grp = model['training']
    if mask == None: mask = grp.attrs['mask']
    network = cls(model.attrs['n_in'], model.attrs['n_out'], mask)
    network.bidirectional = model.attrs['bidirectional']
    network.L1_reg = grp.attrs['L1_reg']
    network.L2_reg = grp.attrs['L2_reg']
    network.hidden = {}
    network.params = []
    network.L1 = T.constant(0)
    network.L2 = T.constant(0)
    layer = model.attrs['output']
    activations = { 'logistic' : T.nnet.sigmoid,
                    'tanh' : T.tanh,
                    'relu': lambda z : (T.sgn(z) + 1) * z * 0.5,
                    'identity' : lambda z : z,
                    'one' : lambda z : 1,
                    'zero' : lambda z : 0,
                    'softsign': lambda z : z / (1.0 + abs(z)),
                    'softsquare': lambda z : 1 / (1.0 + z * z),
                    'maxout': lambda z : T.max(z, axis = 0),
                    'sin' : T.sin,
                    'cos' : T.cos }
    network.recurrent = False

    def traverse(model, layer, network):
      n_in = 0
      if 'from' in model[layer].attrs:
        x_in = []
        for s in model[layer].attrs['from'].split(','):
          traverse(model, s, network)
          x_in.append(network.hidden[s].output)
          n_in += network.hidden[s].n_out
        if len(x_in) == 1: x_in = x_in[0]
      else:
        x_in = network.x
        n_in = network.n_in
      if layer != model.attrs['output']:
        cl = model[layer].attrs['class']
        act = model[layer].attrs['activation']
        params = { 'source': x_in,
                   'n_in': n_in,
                   'n_out': model[layer].attrs['n_out'],
                   'activation': activations[act],
                   'dropout': model[layer].attrs['dropout'],
                   'name': layer,
                   'mask': model[layer].attrs['mask'] }
        network.recurrent = network.recurrent or (cl != 'hidden')
        if cl == 'hidden':
          network.add_layer(layer, ForwardLayer(**params), act)
        else:
          params['truncation'] = model[layer].attrs['truncation']
          params['reverse'] = model[layer].attrs['reverse']
          if cl == 'recurrent':
            network.add_layer(layer, RecurrentLayer(index = network.i, **params), act)
          elif cl == 'lstm':
            network.add_layer(layer, LstmLayer(index = network.i, sharpgates = model[layer].attrs['sharpgates'], truncation = model[layer].attrs['truncation'], **params), act)
        for a in model[layer].attrs:
          network.hidden[layer].attrs[a] = model[layer].attrs[a]
    output = model.attrs['output']
    traverse(model, output, network)
    sources = model[output].attrs['from'].split(',')
    network.make_classifier(sources, grp.attrs['loss'], model[output].attrs['dropout'])
    return network
    
  def add_hidden(self, name, size, layer_type = 'forward', activation = ("tanh", T.tanh)):
    self.hidden_info.append((layer_type, size, activation, name))

  def add_layer(self, name, layer, activation):
    self.hidden[name] = layer
    self.hidden[name].set_attr('activation', activation)
    if 'recurrent' in layer.attrs.keys() and layer.attrs['recurrent']:
      self.L1 += abs(self.hidden[name].W_re.sum())
      self.L2 += (self.hidden[name].W_re ** 2).sum()
    self.L1 += abs(self.hidden[name].W_in.sum())
    self.L2 += (self.hidden[name].W_in ** 2).sum()
    self.params += self.hidden[name].params.values()

  def make_classifier(self, sources, loss, dropout = 0):
    self.gparams = self.params[:]
    self.loss = loss
    self.output = OutputLayer(sources = [self.hidden[s] for s in sources], index = self.i, n_out = self.n_out, loss = loss, dropout = dropout, mask = self.mask, name = "output")
    self.output.set_attr('from', ",".join(sources))
    for W in self.output.W_in:
      self.L1 += abs(W.sum())
      self.L2 += (W ** 2).sum()
    self.params += self.output.params.values()
    self.gparams += self.output.params.values()
    targets = self.c if self.loss == 'ctc' else self.y
    self.errors = self.output.errors(targets)
    self.cost, self.known_grads = self.output.cost(targets)
    self.objective = self.cost + self.L1_reg * self.L1 + self.L2_reg * self.L2 #+ entropy * self.output.entropy()
    #if hasattr(LstmLayer, 'sharpgates'):
      #self.objective += entropy * (LstmLayer.sharpgates ** 2).sum()
    #self.jacobian = T.jacobian(self.output.z, self.x)
  
  def initialize(self, loss, L1_reg, L2_reg, dropout = 0, bidirectional = True, truncation = -1, sharpgates = 'none', entropy = 0):
    self.hidden = {}
    self.params = []
    n_in = self.n_in
    x_in = self.x
    self.L1 = T.constant(0)
    self.L2 = T.constant(0)
    self.L1_reg = L1_reg
    self.L2_reg = L2_reg
    self.recurrent = False
    self.bidirectional = bidirectional
    if hasattr(LstmLayer, 'sharpgates'):
      del LstmLayer.sharpgates
    # create forward layers
    for info, drop in zip(self.hidden_info, dropout[:-1]):
      params = { 'source': x_in, 'n_in': n_in, 'n_out': info[1], 'activation': info[2][1], 'dropout': drop, 'name': info[3], 'mask': self.mask }
      name = params['name']
      if info[0] == 'forward':
        self.add_layer(name, ForwardLayer(**params), info[2][0])
      else:
        self.recurrent = True
        params['index'] = self.i
        params['truncation'] = truncation
        if self.bidirectional:
          params['name'] = info[3] + "_fw"
        name = params['name']
        if info[0] == 'recurrent':
          self.add_layer(name, RecurrentLayer(**params), info[2][0])
        elif info[0] == 'lstm':
          self.add_layer(name, LstmLayer(sharpgates = sharpgates, **params), info[2][0])
        elif info[0] == 'gatelstm':
          self.add_layer(name, GateLstmLayer(sharpgates = sharpgates, **params), info[2][0])
        elif info[0] == 'peep_lstm':
          self.add_layer(name, LstmPeepholeLayer(**params), info[2][0])
        else: assert False, "invalid layer type: " + info[0]
      if self.hidden[name].source != self.x:
        self.hidden[name].set_attr('from', pname)
      pname = name
      n_in = info[1]
      x_in = self.hidden[name].output
    sources = [name]
    # create backward layers
    assert self.recurrent or not self.bidirectional, "non-recurrent networks can not be bidirectional"
    if self.bidirectional:
      n_in = self.n_in
      x_in = self.x
      for info, drop in zip(self.hidden_info, dropout[:-1]):
        params = { 'source': x_in, 'n_in': n_in, 'n_out': info[1], 'activation': info[2][1], 'dropout': drop, 'name': info[3] + "_bw", 'mask': self.mask }
        name = params['name']
        if info[0] == 'forward':
          self.hidden[name] = self.add_layer(name, ForwardLayer(**params), info[2][0])
        else:
          params['index'] = self.i
          if self.bidirectional:
            params['reverse'] = True
          name = params['name']
          if info[0] == 'recurrent':
            self.add_layer(name, RecurrentLayer(**params), info[2][0])
          elif info[0] == 'lstm':
            self.add_layer(name, LstmLayer(sharpgates = sharpgates, **params), info[2][0])
          elif info[0] == 'gatelstm':
            self.add_layer(name, GateLstmLayer(sharpgates = sharpgates, **params), info[2][0])
          elif info[0] == 'peep_lstm':
            self.add_layer(name, LstmPeepholeLayer(**params), info[2][0])
          else: assert False, "invalid layer type: " + info[0]
        if self.hidden[name].source != self.x:
          self.hidden[name].set_attr('from', pname)
        pname = name
        n_in = info[1]
        x_in = self.hidden[name].output
      sources.append(name)
    self.make_classifier(sources, loss, dropout[-1])

  def num_params(self):
    return sum([self.hidden[h].num_params() for h in self.hidden]) + self.output.num_params()
  
  def get_params(self):
    params = { self.output.name : self.output.get_params() }
    for h in self.hidden:
      params[h] = self.hidden[h].get_params()
    return params
  
  def set_params(self, params):
    self.output.set_params(params[self.output.name])
    for h in self.hidden:
      self.hidden[h].set_params(params[h])
  
  def save(self, model, epoch):
    grp = model.create_group('training')
    grp.attrs['L1_reg'] = self.L1_reg
    grp.attrs['L2_reg'] = self.L2_reg
    grp.attrs['loss'] = self.loss
    grp.attrs['mask'] = self.mask
    model.attrs['epoch'] = epoch
    model.attrs['bidirectional'] = self.bidirectional
    model.attrs['output'] = self.output.name
    model.attrs['n_in'] = self.n_in
    model.attrs['n_out'] = self.n_out
    for h in self.hidden:
      self.hidden[h].save(model)
    self.output.save(model)
  
  def load(self, model):
    epoch = model.attrs['epoch']
    self.bidirectional = model.attrs['bidirectional']
    for name in self.hidden:
      assert name in model, "unable to load layer " + name
      self.hidden[name].load(model)
    self.output.load(model)
    return epoch
