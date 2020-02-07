import glob
import json
import time
from random import shuffle
from threading import Thread
import queue
import torch
from graph_matching.datasets.vocab import PAD_TOKEN, UNKNOWN_TOKEN, DECODING_START, DECODING_END


class Batch(object):
    def __init__(self, examples, config, vocab, struct_dist):

        self.config = config

        branch_batch_size = config['graph_structure_net']['branch_batch_size']
        sen_batch_size = config['graph_structure_net']['sen_batch_size']
        max_enc_steps = config['graph_structure_net']['max_enc_steps']
        max_dec_steps = config['graph_structure_net']['max_dec_steps']
        sen_hidden_dim = config['graph_structure_net']['sen_hidden_dim']

        self.enc_batch = torch.zeros(branch_batch_size,
                                     sen_batch_size,
                                     max_enc_steps,
                                     dtype=torch.int64)
        self.enc_lens = torch.zeros(branch_batch_size,
                                    sen_batch_size,
                                    dtype=torch.int32)
        self.attn_mask = -1e10 * torch.ones(
            branch_batch_size,
            sen_batch_size,
            max_enc_steps,
            dtype=torch.float32)  # attention mask batch
        self.branch_lens_mask = torch.zeros(branch_batch_size,
                                            sen_batch_size,
                                            sen_batch_size,
                                            dtype=torch.float32)

        self.dec_batch = torch.zeros(branch_batch_size,
                                     max_dec_steps,
                                     dtype=torch.int64)  # decoder input
        self.target_batch = torch.zeros(
            branch_batch_size, max_dec_steps,
            dtype=torch.int32)  # target sequence index batch
        self.padding_mark = torch.zeros(
            branch_batch_size, max_dec_steps,
            dtype=torch.float32)  # target mask batch
        # self.tgt_batch_len = torch.zeros(config.branch_batch_size, dtype=torch.int32)      # target batch length

        self.state_matrix = torch.zeros(branch_batch_size,
                                        sen_batch_size,
                                        sen_batch_size,
                                        dtype=torch.int64)
        self.struct_conv = torch.zeros(branch_batch_size,
                                       sen_batch_size,
                                       sen_batch_size,
                                       dtype=torch.int64)
        self.struct_dist = torch.zeros(branch_batch_size,
                                       sen_batch_size,
                                       sen_batch_size,
                                       dtype=torch.int64)

        self.relate_user = torch.zeros(branch_batch_size,
                                       sen_batch_size,
                                       sen_batch_size,
                                       dtype=torch.int64)

        self.mask_emb = torch.zeros(branch_batch_size,
                                    sen_batch_size,
                                    sen_batch_size,
                                    sen_hidden_dim * 2,
                                    dtype=torch.float32)
        self.mask_user = torch.zeros(branch_batch_size,
                                     sen_batch_size,
                                     sen_batch_size,
                                     sen_hidden_dim * 2,
                                     dtype=torch.float32)
        mask_tool = torch.ones(sen_hidden_dim * 2, dtype=torch.float32)

        # self.tgt_index = torch.zeros(config.branch_batch_size, config.sen_batch_size, dtype=torch.int32)
        self.tgt_index = torch.zeros(branch_batch_size, dtype=torch.int64)

        self.context = []
        self.response = []

        enc_lens_mid = []

        # self.small_or_large = []

        for i, ex in enumerate(examples):
            # self.small_or_large.append(ex.small_large)

            for j, branch in enumerate(ex.enc_input):
                self.enc_batch[i, j, :] = torch.LongTensor(branch[:])

            for enc_idx, enc_len in enumerate(ex.enc_len):
                self.enc_lens[i][enc_idx] = enc_len
                if enc_len != 0:
                    # initialization of state_matrix
                    self.state_matrix[i][enc_idx][
                        enc_idx] = sen_batch_size * i + enc_idx + 1
                for j in range(enc_len):
                    self.attn_mask[i][enc_idx][j] = 0

            # the relaton of sentence
            for pair_struct in ex.context_struct:
                # struct_conv represent the relation of sentence A@B
                self.struct_conv[i][pair_struct[1]][pair_struct[0]] = 1
                self.mask_emb[i][pair_struct[1]][pair_struct[0]][:] = mask_tool
            # the relation of same user
            for pair_relat in ex.relation_pair:
                self.relate_user[i][pair_relat[1]][pair_relat[0]] = 1
                self.mask_user[i][pair_relat[1]][pair_relat[0]][:] = mask_tool

            for j in range(ex.branch_len):
                # self.struct_dist[i, :, :] = struct_dist[j]
                for k in range(ex.branch_len):
                    self.branch_lens_mask[i][j][k] = 1

            # decoder input
            self.dec_batch[i, :] = torch.LongTensor(ex.dec_input)

            # train target
            self.target_batch[i, :] = torch.IntTensor(ex.dec_target)

            # decoder padding
            for j in range(ex.dec_len):
                self.padding_mark[i][j] = 1

            # response idx
            self.tgt_index[i] = ex.tgt_idx + i * sen_batch_size

            # TODO: add prediction
            self.struct_dist[i, :, :] = 0

            self.context.append(ex.original_context)
            self.response.append(ex.original_response)

        self.enc_lens = self.enc_lens.view(branch_batch_size * sen_batch_size)
        # self.enc_lens[:] = enc_lens_mid


class Batcher(object):
    """A class to generate mini-batches of data.
    """
    BATCH_QUEUE_MAX = 5

    def __init__(self, data_path, vocab, config):
        """Constructor.
        
        Args:
            data_path ([type]): [description]
            vocab ([type]): [description]
            config ([type]): [description]
        """
        print("data_path: ", data_path)

        self.data_path = data_path
        self.vocab = vocab
        self.config = config

        self.batch_queue = queue.Queue(self.BATCH_QUEUE_MAX)
        self.input_queue = queue.Queue(
            self.BATCH_QUEUE_MAX *
            self.config['graph_structure_net']['branch_batch_size'])

        # with open('/'.join(data_path.split('/')[:-1]) + '/' + 'pred_struct_dist.pkl', 'r') as f_pred:
        # self.struct_dist = pkl.load(f_pred)
        self.struct_dist = None

        if config['mode'] == 'eval':
            self.eval_num = 0

        self.num_input_threads = 1
        self.num_batch_threads = 1
        self.cache_size = 5

        self.input_threads = []
        for _ in range(self.num_input_threads):
            self.input_threads.append(Thread(target=self._fill_input_queue))
            self.input_threads[-1].daemon = True
            self.input_threads[-1].start()

        self.batch_threads = []
        for _ in range(self.num_batch_threads):
            self.batch_threads.append(Thread(target=self._fill_batch_queue))
            self.batch_threads[-1].daemon = True
            self.batch_threads[-1].start()

        self.watch_thread = Thread(target=self._watch_threads)
        self.watch_thread.daemon = True
        self.watch_thread.start()

    def _next_batch(self):
        """Return a Batch from the batch queue.
        """
        if self.config['mode'] == 'eval':
            if self.eval_num > 5000 / self.config['graph_structure_net'][
                    'branch_batch_size']:
                self.eval_num = 0
                return None
            else:
                self.eval_num += 1

        batch = self.batch_queue.get()
        return batch

    def _fill_input_queue(self):
        """Reads data from file and put into input queue
        """
        while True:
            file_list = glob.glob(self.data_path)
            if self.config['mode'] == 'decode':
                file_list = sorted(file_list)
            else:
                shuffle(file_list)

            for f in file_list:
                with open(f, 'rb') as reader:
                    for record in reader:
                        record = RecordMaker(record, self.vocab, self.config)
                        self.input_queue.put(record)

    def _fill_batch_queue(self):
        """Get data from input queue and put into batch queue
        """
        while True:
            if self.config['mode'] == 'decode':
                ex = self.input_queue.get()
                b = [
                    ex for _ in range(self.config['graph_structure_net']
                                      ['branch_batch_size'])
                ]
                self.batch_queue.put(
                    Batch(b, self.config, self.vocab, self.struct_dist))
            else:
                inputs = []
                for _ in range(
                        self.config['graph_structure_net']['branch_batch_size']
                        * self.cache_size):
                    inputs.append(self.input_queue.get())

                batches = []
                for i in range(
                        0, len(inputs), self.config['graph_structure_net']
                    ['branch_batch_size']):
                    batches.append(
                        inputs[i:i + self.config['graph_structure_net']
                               ['branch_batch_size']])
                if self.config['mode'] not in ['eval', 'decode']:
                    shuffle(batches)
                for b in batches:
                    self.batch_queue.put(
                        Batch(b, self.config, self.vocab, self.struct_dist))

    def _watch_threads(self):
        """Watch input queue and batch queue threads and restart if dead."""
        while True:
            time.sleep(60)
            for idx, t in enumerate(self.input_threads):
                if not t.is_alive():
                    # tf.logging.error('Found input queue thread dead. Restarting.')
                    new_t = Thread(target=self._fill_input_queue)
                    self.input_threads[idx] = new_t
                    new_t.daemon = True
                    new_t.start()
            for idx, t in enumerate(self.batch_threads):
                if not t.is_alive():
                    # tf.logging.error('Found batch queue thread dead. Restarting.')
                    new_t = Thread(target=self._fill_batch_queue)
                    self.batch_threads[idx] = new_t
                    new_t.daemon = True
                    new_t.start()


class RecordMaker(object):
    def __init__(self, record, vocab, config):
        self.config = config

        start_id = vocab._word2id(DECODING_START)
        end_id = vocab._word2id(DECODING_END)
        self.pad_id = vocab._word2id(PAD_TOKEN)
        max_enc_steps = config['graph_structure_net']['max_enc_steps']
        max_dec_steps = config['graph_structure_net']['max_dec_steps']

        ### load data from the json string
        record = json.loads(record)
        context_list = record['context']  # the context
        response = record['answer']  # the answer
        self.tgt_idx = record[
            'ans_idx']  # The index of the context sentence corresponding to the answer
        self.context_struct = record[
            'relation_at']  # the relation structure of the context sentence
        self.relation_pair = record[
            'relation_user']  # the relation structure of the user (speakers)

        ### encoder
        context_words = []
        for context in context_list:
            words = context.strip().split()[:max_enc_steps]
            context_words.append(words)

        self.branch_len = len(context_words)
        self.enc_len = []
        self.enc_input = []
        for words in context_words:
            self.enc_len.append(len(words))
            self.enc_input.append([vocab._word2id(w) for w in words] +
                                  [self.pad_id] * (max_enc_steps - len(words)))

        self.pad_sent = [self.pad_id for _ in range(max_enc_steps)
                         ]  # the sentence which only have 'pad_id'
        while len(self.enc_input
                  ) < config['graph_structure_net']['sen_batch_size']:
            self.enc_len.append(0)
            self.enc_input.append(self.pad_sent)

        ### decoder
        response_words = response.strip().split()
        dec_ids = [vocab._word2id(w) for w in response_words]
        # dec_ids lens
        self.dec_len = len(dec_ids) + 1 if (
            len(dec_ids) + 1) < max_dec_steps else max_dec_steps
        # decoder input
        self.dec_input = [start_id] + dec_ids[:max_dec_steps - 1] + \
                         [self.pad_id] * (max_dec_steps - len(dec_ids) - 1)
        # decoder target
        self.dec_target = dec_ids[:max_dec_steps - 1] + [end_id] + \
                          [self.pad_id] * (max_dec_steps - len(dec_ids) - 1)

        self.original_context = ' '.join(context_list)
        self.original_response = response
