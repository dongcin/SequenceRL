import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops
import cPickle
import numpy as np
class RLLstm(object):
    def __init__(self, vocab_num, batch_size, input_size, hidden_size,
                 n_steps, start_token,grad_clip=5.0,
                 learning_rate=0.01,is_sample=True):
        self.vocab_num = vocab_num
        self.batch_size = batch_size
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.n_steps = n_steps
        self.is_sample = is_sample
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
        self.grad_clip = grad_clip
        self.g_params = []
        
        tf.set_random_seed(66)
        
        with tf.variable_scope('basic_lstm'):
            self.Whx = tf.Variable(self.init_matrix([self.vocab_num, self.input_size]))
            self.g_params.append(self.Whx)
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.create_output_unit(self.g_params)  # maps h_t to o_t (output token logits)

        
        self.inputs = tf.placeholder(tf.int32, shape=[self.batch_size, self.n_steps])
        
        
        with tf.device("/cpu:0"):
            self.w2vec = tf.transpose(tf.nn.embedding_lookup(self.Whx, self.inputs), perm=[1, 0, 2])  # seq_length x batch_size x input_size

        ta_emb_x = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.n_steps)
        ta_emb_x = ta_emb_x.unpack(self.w2vec)# inputs                        
        
        # Initial states
        self.h0 = tf.zeros([self.batch_size, self.hidden_size])
        self.h0 = tf.pack([self.h0, self.h0]) #{h0,c0}
        
        # supervised pretraining for LSTM
        l_predictions = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.n_steps,dynamic_size=False, infer_shape=True)
        
        def _pretrain_recurrence(i, x_t, h_tm1, l_predictions):
            h_t = self.g_recurrent_unit(x_t, h_tm1)#{ht,ct}
            o_t = self.g_output_unit(h_t)
            l_predictions = l_predictions.write(i, tf.nn.softmax(o_t))  # batch x vocab_size
            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, l_predictions # both x_tp1 and l_predictions are next token, but x_tp1 is the groundtruth.

        _, _, _, self.l_predictions = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3: i < self.n_steps,
            body=_pretrain_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.Whx, self.start_token),
                       self.h0, l_predictions))

        self.l_predictions = tf.transpose(self.l_predictions.pack(), perm=[1, 0, 2])  # batch_size x seq_length x vocab_size

        self.pretrain_loss = -tf.reduce_sum(
            tf.one_hot(tf.to_int32(tf.reshape(self.inputs, [-1])), self.vocab_num, 1.0, 0.0) * tf.log(
                tf.clip_by_value(tf.reshape(self.l_predictions, [-1, self.vocab_num]), 1e-20, 1.0)
            )
        ) / (self.n_steps * self.batch_size)# (batch_size x seq_length) x vocab_size

        pretrain_opt = self.g_optimizer(self.learning_rate)

        self.pretrain_grad, _ = tf.clip_by_global_norm(tf.gradients(self.pretrain_loss, self.g_params), self.grad_clip)
        self.pretrain_updates = pretrain_opt.apply_gradients(zip(self.pretrain_grad, self.g_params))
                
         # sample sentences from LSTM
        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.n_steps,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.n_steps,
                                             dynamic_size=False, infer_shape=True)
        
        def _g_recurrence(i, x_t, h_tm1, gen_o, gen_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)  # hidden_memory_tuple
            o_t = self.g_output_unit(h_t)  # batch x vocab , logits not prob
            log_prob = tf.log(tf.nn.softmax(o_t))            
            if self.is_sample:
                next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)
            else:
                next_token = tf.cast(tf.reshape(tf.argmax(log_prob, 1), [self.batch_size]), tf.int32)                
            x_tp1 = tf.nn.embedding_lookup(self.Whx, next_token)  # batch x input_size
            gen_o = gen_o.write(i, tf.reduce_sum(tf.mul(tf.one_hot(next_token, self.vocab_num, 1.0, 0.0),
                                                             tf.nn.softmax(o_t)), 1))# batch_size x vocab_num => [batch_size] , prob
            gen_x = gen_x.write(i, next_token)  # indices, batch_size
            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.n_steps,
            body=_g_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.Whx, self.start_token), self.h0, gen_o, gen_x))

        self.gen_x = self.gen_x.pack()  # seq_length x batch_size
        self.gen_x = tf.transpose(self.gen_x, perm=[1, 0])  # batch_size x seq_length
        
        
        # sample sentences from MC with LSTM
        self.given_steps = tf.placeholder(tf.int32)
        
        self.mc_inputs = tf.placeholder(tf.int32, shape=[self.batch_size, self.n_steps])        
        
        with tf.device("/cpu:0"):
            self.mc_w2vec = tf.transpose(tf.nn.embedding_lookup(self.Whx, self.mc_inputs), perm=[1, 0, 2])  # seq_length x batch_size x input_size

        mc_ta_emb_x = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.n_steps)
        mc_ta_emb_x = mc_ta_emb_x.unpack(self.mc_w2vec)# inputs                        
        
        mc_ta_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.n_steps)
        mc_ta_x = mc_ta_x.unpack(tf.transpose(self.mc_inputs, perm=[1, 0]))
        
        
        gen_mc_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.n_steps,
                                             dynamic_size=False, infer_shape=True)

        # When current index i < given_steps, use the provided tokens as the input at each time step
        def _g_recurrence_1(i, x_t, h_tm1, given_steps, gen_mc_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)  # hidden_memory_tuple
            x_tp1 = mc_ta_emb_x.read(i)
            gen_mc_x = gen_mc_x.write(i, mc_ta_x.read(i))
            return i + 1, x_tp1, h_t, given_steps, gen_mc_x

        # When current index i >= given_steps, start roll-out, use the output at time step t as the input at time step t+1
        def _g_recurrence_2(i, x_t, h_tm1, given_steps, gen_mc_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)  # hidden_memory_tuple
            o_t = self.g_output_unit(h_t)  # batch x vocab , logits not prob
            log_prob = tf.log(tf.nn.softmax(o_t))
            next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)
            x_tp1 = tf.nn.embedding_lookup(self.Whx, next_token)  # batch x emb_dim
            gen_mc_x = gen_mc_x.write(i, next_token)  # indices, batch_size
            return i + 1, x_tp1, h_t, given_steps, gen_mc_x

        i, x_t, h_tm1, given_steps, self.gen_mc_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, given_steps, _4: i < given_steps,
            body=_g_recurrence_1,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.Whx, self.start_token), self.h0, self.given_steps, gen_mc_x))

        _, _, _, _, self.gen_mc_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.n_steps,
            body=_g_recurrence_2,
            loop_vars=(i, x_t, h_tm1, given_steps, self.gen_mc_x))

        self.gen_mc_x = self.gen_mc_x.pack()  # seq_length x batch_size
        self.gen_mc_x = tf.transpose(self.gen_mc_x, perm=[1, 0])  # batch_size x seq_length
        
        #######################################################################################################
        #  Unsupervised Training with Policy gradient
        #######################################################################################################        
        self.rewards = tf.placeholder(tf.float32, shape=[self.batch_size, self.n_steps]) # get from rollout policy and discriminator

        self.g_loss = -tf.reduce_sum(
            tf.reduce_sum(
                tf.one_hot(tf.to_int32(tf.reshape(self.inputs, [-1])), self.vocab_num, 1.0, 0.0) * tf.log(
                    tf.clip_by_value(tf.reshape(self.l_predictions, [-1, self.vocab_num]), 1e-20, 1.0)
                ), 1) * tf.reshape(self.rewards, [-1])
        )
        g_opt = self.g_optimizer(self.learning_rate)

        self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params), self.grad_clip)
        self.g_updates = g_opt.apply_gradients(zip(self.g_grad, self.g_params))  
        
    def get_reward(self, sess, samples, sample_cnt=5, discriminator=None):
        rewards = np.zeros(shape=(self.n_steps,self.batch_size),dtype=np.float32) 
        ypred = np.ones(shape=(self.batch_size,))             
        for i in range(sample_cnt):
            for n_steps in range(1, self.n_steps + 1):#[1,20]
                samples = sess.run(self.gen_mc_x, feed_dict={self.mc_inputs: samples,self.given_steps:n_steps})
                rewards[n_steps - 1,:] += ypred
#            feed = {discriminator.input_x: samples, discriminator.dropout_keep_prob: 1.0}                
#            ypred_for_auc = sess.run(discriminator.ypred_for_auc, feed)
#            ypred = np.array([item[1] for item in ypred_for_auc])
#                if i == 0:
#                    rewards.append(ypred)
#                else:
#                    rewards[n_steps - 1] += ypred
        rewards = np.transpose(np.array(rewards)) / (1.0 * sample_cnt)  # batch_size x seq_length
        return rewards
        
    def generate(self, sess):
        outputs = sess.run(self.gen_x)
        return outputs
        
    def unsupervised_train_step(self, sess,rewards):
        outputs = sess.run([self.g_updates, self.g_loss],feed_dict={self.rewards: rewards})
        return outputs
           
            
    def pretrain_step(self, sess, x):
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss], feed_dict={self.inputs: x})
        return outputs

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=0.1)

    def init_vector(self, shape):
        return tf.zeros(shape)

    def create_recurrent_unit(self, params):
        # Weights and Bias for input and hidden tensor
        self.Wi = tf.Variable(self.init_matrix([self.input_size, self.hidden_size]))
        self.Ui = tf.Variable(self.init_matrix([self.hidden_size, self.hidden_size]))
        self.bi = tf.Variable(self.init_matrix([self.hidden_size]))

        self.Wf = tf.Variable(self.init_matrix([self.input_size, self.hidden_size]))
        self.Uf = tf.Variable(self.init_matrix([self.hidden_size, self.hidden_size]))
        self.bf = tf.Variable(self.init_matrix([self.hidden_size]))

        self.Wog = tf.Variable(self.init_matrix([self.input_size, self.hidden_size]))
        self.Uog = tf.Variable(self.init_matrix([self.hidden_size, self.hidden_size]))
        self.bog = tf.Variable(self.init_matrix([self.hidden_size]))

        self.Wc = tf.Variable(self.init_matrix([self.input_size, self.hidden_size]))
        self.Uc = tf.Variable(self.init_matrix([self.hidden_size, self.hidden_size]))
        self.bc = tf.Variable(self.init_matrix([self.hidden_size]))
        params.extend([
            self.Wi, self.Ui, self.bi,
            self.Wf, self.Uf, self.bf,
            self.Wog, self.Uog, self.bog,
            self.Wc, self.Uc, self.bc])

        def unit(x, hidden_memory_tm1):
            previous_hidden_state, c_prev = tf.unpack(hidden_memory_tm1)

            # Input Gate
            i = tf.sigmoid(
                tf.matmul(x, self.Wi) +
                tf.matmul(previous_hidden_state, self.Ui) + self.bi
            )

            # Forget Gate
            f = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                tf.matmul(previous_hidden_state, self.Uf) + self.bf
            )

            # Output Gate
            o = tf.sigmoid(
                tf.matmul(x, self.Wog) +
                tf.matmul(previous_hidden_state, self.Uog) + self.bog
            )

            # New Memory Cell
            c_ = tf.nn.tanh(
                tf.matmul(x, self.Wc) +
                tf.matmul(previous_hidden_state, self.Uc) + self.bc
            )

            # Final Memory cell
            c = f * c_prev + i * c_

            # Current Hidden state
            current_hidden_state = o * tf.nn.tanh(c)

            return tf.pack([current_hidden_state, c])

        return unit    
    def create_output_unit(self, params):
        self.Wo = tf.Variable(self.init_matrix([self.hidden_size, self.vocab_num]))
        self.bo = tf.Variable(self.init_matrix([self.vocab_num]))
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            hidden_state, c_prev = tf.unpack(hidden_memory_tuple)
            # hidden_state : batch x hidden_size
            logits = tf.matmul(hidden_state, self.Wo) + self.bo
            # output = tf.nn.softmax(logits)
            return logits

        return unit

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)
        
    def save_model(self,sess,model_path,global_step):        

        outputs = sess.run([self.Whx,self.Wi, self.Ui, self.bi,
                            self.Wf, self.Uf, self.bf,
                            self.Wog, self.Uog, self.bog,
                            self.Wc, self.Uc, self.bc,self.Wo, self.bo])
        cPickle.dump(outputs,open(model_path + '-' + str(global_step) + '.pkl', 'wb'),-1)
       
    def restore_model(self,sess,model_path):
        params = cPickle.load(open(model_path))
        
        self.Whx = tf.Variable(params[0])
        
        self.Wi = tf.Variable(params[1])
        self.Ui = tf.Variable(params[2])
        self.bi = tf.Variable(params[3])

        self.Wf = tf.Variable(params[4])
        self.Uf = tf.Variable(params[5])
        self.bf = tf.Variable(params[6])

        self.Wog = tf.Variable(params[7])
        self.Uog = tf.Variable(params[8])
        self.bog = tf.Variable(params[9])

        self.Wc = tf.Variable(params[10])
        self.Uc = tf.Variable(params[11])
        self.bc = tf.Variable(params[12])
        
        self.Wo = tf.Variable(params[13])
        self.bo = tf.Variable(params[14])
        