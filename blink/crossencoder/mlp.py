import torch
from torch import nn, optim
import torch.nn.functional as F
import os
from pytorch_transformers import PreTrainedModel, PretrainedConfig
from pytorch_transformers.modeling_bert import (
    BertPreTrainedModel,
    BertConfig,
    BertModel,
)
import sys
sys.path.append('/mnt/f/BLINK')
from blink.common.ranker_base import get_model_obj
from pytorch_transformers.tokenization_bert import BertTokenizer
from pytorch_transformers.tokenization_roberta import RobertaTokenizer
from blink.common.params import BlinkParser
from torchsummary import summary

def load_mlp(params):
    # Init model
    crossencoder = MlpModel(params)
    return crossencoder
k=64
input_size=768*2 #(64,256)
## model이랑 module, ranker 나눠서 modeule이랑 ranker에서 model 호출
class MlpModel(nn.Module):
    def __init__(self,params, top_k=k, model_input=input):
        super(MlpModel, self).__init__()
        self.params=params
        self.device=torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )
        self.n_gpu = torch.cuda.device_count()
        self.data_parallel=1
        self.tokenizer=BertTokenizer.from_pretrained(
                "bert-base-cased"
            )
        self.build_model()
        self.top_k = top_k
        self.model = self.model.to(self.device)
        # self.model = torch.nn.DataParallel(self.model)
    def build_model(self):
        self.model=MlpModule(self.params)
    def save_model(self, output_dir):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        torch.save(self.model.state_dict(), output_dir)
    def load_model(self, fname, cpu=False):
        if cpu:
            state_dict = torch.load(fname, map_location=lambda storage, location: "cpu")
        else:
            state_dict = torch.load(fname)
        self.model.load_state_dict(state_dict)
    def forward(self, input, label_input, context_length, evaluate = False):
        # summary(self.model, input_size=(1, 2, 1024))

        # input shape: (batch size, top_k, 2, bert_hidden_dimension)
        # score shape: (batch_size, top_k)
        if not self.params["binary_loss"]:
            scores=torch.squeeze(self.model(input), dim=2)
            loss=F.cross_entropy(scores, label_input, reduction="mean")
        else:
            if not evaluate:
                num_samples = 10 # the number of negative samples
                label_input = torch.unsqueeze(label_input, dim = 1)
                # Making target tensor for BCELoss
                # Target tensor shape: (batch_size, num_samples + 1)
                # Target tensor looks like: [[1, 0, ... , 0], ... [1, 0, ..., 0]]
                target = torch.zeros((input.size(0), num_samples + 1), device = torch.device('cuda'), dtype = torch.float32) 
                target[torch.arange(target.size(0)),0] = 1 
                # sampled input shape: (batch_size, num_samples + 1, 2, hidden_dim)
                # sampled input consists of gold_input and negative_input
                # gold_input : label-th tensor from each candidate
                gold_input = torch.gather(input, 1, label_input.view(input.size(0), 1, 1, 1).expand(input.size(0), 1, input.size(2), input.size(3)))
                # negative input: negative sample tensor whose shape is (batch_size, num_samples, 2, hidden_dim)
                masked_input = torch.ones_like(input).scatter(1, label_input.view(input.size(0), 1, 1, 1).expand(input.size(0), 1, input.size(2), input.size(3)), 0) # negative sample에 1 assign
                idxs_ = masked_input.nonzero()[:, 1].reshape(-1, input.size(1) - label_input.size(1), input.size(2), input.size(3))
                negative_input = torch.gather(input, 1, idxs_)
                # uniform sampling using 'torch.randperm'
                negative_indices = torch.randperm(self.top_k-1)[:num_samples]
                negative_input = negative_input[:,negative_indices,:,:] 
                # concatenate gold_input and negative_input
                sampled_input = torch.cat((gold_input, negative_input), dim = 1)
                # Get scores by feeding sampled_input
                scores = torch.squeeze(self.model(sampled_input), dim = 2)
                # Assigning weights on BCELoss
                # 0.1 for negatives and 1 for gold
                weights = (0.1)*torch.ones((input.size(0), num_samples + 1), device = torch.device('cuda'))
                weights[torch.arange(weights.size(0)),0] = 1
                criterion = torch.nn.BCEWithLogitsLoss(weight = weights)
                loss = criterion(scores, target)
                print("loss: ", loss)
            else:
                scores=torch.squeeze(self.model(input), dim=2)
                loss=F.cross_entropy(scores, label_input, reduction="mean")
        torch.set_printoptions(threshold=10_000)
        return loss, scores
class MlpModule(nn.Module):
    def __init__(self,params, top_k=k, model_input=input_size):
        super(MlpModule, self).__init__()
        self.params=params
        self.input_size=model_input 
        if params["bert_model"]=="bert-base-cased":
            self.input_size = 768*2
        self.fc=nn.Linear(self.input_size, self.input_size)
        self.fc2=nn.Linear(self.input_size, 1)
        self.dropout=nn.Dropout(0.1)
        self.layers = nn.ModuleList()
        if params["act_fn"] == "softplus":
            self.act_fn = nn.Softplus()
        if params["act_fn"] == "sigmoid":
            self.act_fn = nn.Sigmoid()
        elif params["act_fn"] == "tanh":
            self.act_fn = nn.Tanh()
        self.current_dim = self.input_size
        if not self.params["decoder"]:
            if self.params["dim_red"]:
                for i in range(self.params["layers"]):
                    self.layers.append(nn.Linear(self.current_dim, self.params["dim_red"]))
                    self.current_dim = self.params["dim_red"]
            else: 
                for i in range(self.params["layers"]):
                    self.layers.append(nn.Linear(self.current_dim, self.current_dim))
        else:
            self.layers.append(nn.Linear(int(self.current_dim), int(self.params["dim_red"])))
            self.current_dim = self.params["dim_red"]
            for i in range(self.params["layers"]-1):
                self.layers.append(nn.Linear(int(self.current_dim), int(self.current_dim/2)))
                self.current_dim /= 2

        self.layers.append(nn.Linear(int(self.current_dim), 1))

        ## projection과 linear mapping 구분
        # relu나 sigmoid나 tanh
    def forward(self, input):
        input = torch.flatten(input, start_dim = 2)
        for i, layer in enumerate(self.layers[:-1]):
            input = self.act_fn(layer(self.dropout(input)))
        input = self.layers[-1](self.dropout(input))
        return input
        # if not decoding: 
        #     if not self.params["dim_red"]:
        #         # print("1-1")
        #         for i in range(self.params["layers"]):
        #             # print("1-2")
        #             input=self.linear(input)
        #         return self.linear2(input)
        #     else:
        #         # print("2-1")
        #         input=self.linear_red1(input)
        #         for i in range(self.params["layers"]-1):
        #             # print("2-2")
        #             input=self.linear_red2(input)
        #         input = self.linear_red3(input) 
        #         return input
        # else:
        #     input=self.linear_red1(input)
        #     for i in range(self.params["layers"]-1):
        #         input_shape = input.size(2)
        #         fc_red4 = nn.Linear(input_shape, input_shape/2)
        #         input = self.dropout(input)
        #         input = fc_red4(input)
        #         input = self.act_fn(input)
        #     input_shape = input.size(2)            
        #     fc_red5 = nn.Linear(input_shape, 1)
        #     input = self.dropout(input)
        #     input = fc_red5(input)
        #     input = self.act_fn(input)
            # return input

# class BertPreTrainedModel(PreTrainedModel):
#     """ An abstract class to handle weights initialization and
#         a simple interface for dowloading and loading pretrained models.
#     """
#     config_class = BertConfig
#     pretrained_model_archive_map = BERT_PRETRAINED_MODEL_ARCHIVE_MAP
#     load_tf_weights = load_tf_weights_in_bert
#     base_model_prefix = "bert"

#     def __init__(self, *inputs, **kwargs):
#         super(BertPreTrainedModel, self).__init__(*inputs, **kwargs)

#     def init_weights(self, module):
#         """ Initialize the weights.
#         """
#         if isinstance(module, (nn.Linear, nn.Embedding)):
#             # Slightly different from the TF version which uses truncated_normal for initialization
#             # cf https://github.com/pytorch/pytorch/pull/5617
#             module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
#         elif isinstance(module, BertLayerNorm):
#             module.bias.data.zero_()
#             module.weight.data.fill_(1.0)
#         if isinstance(module, nn.Linear) and module.bias is not None:
#             module.bias.data.zero_()

# class BertConfig(PretrainedConfig):
#     r"""
#         :class:`~pytorch_transformers.BertConfig` is the configuration class to store the configuration of a
#         `BertModel`.


#         Arguments:
#             vocab_size_or_config_json_file: Vocabulary size of `inputs_ids` in `BertModel`.
#             hidden_size: Size of the encoder layers and the pooler layer.
#             num_hidden_layers: Number of hidden layers in the Transformer encoder.
#             num_attention_heads: Number of attention heads for each attention layer in
#                 the Transformer encoder.
#             intermediate_size: The size of the "intermediate" (i.e., feed-forward)
#                 layer in the Transformer encoder.
#             hidden_act: The non-linear activation function (function or string) in the
#                 encoder and pooler. If string, "gelu", "relu" and "swish" are supported.
#             hidden_dropout_prob: The dropout probabilitiy for all fully connected
#                 layers in the embeddings, encoder, and pooler.
#             attention_probs_dropout_prob: The dropout ratio for the attention
#                 probabilities.
#             max_position_embeddings: The maximum sequence length that this model might
#                 ever be used with. Typically set this to something large just in case
#                 (e.g., 512 or 1024 or 2048).
#             type_vocab_size: The vocabulary size of the `token_type_ids` passed into
#                 `BertModel`.
#             initializer_range: The sttdev of the truncated_normal_initializer for
#                 initializing all weight matrices.
#             layer_norm_eps: The epsilon used by LayerNorm.
#     """
#     pretrained_config_archive_map = BERT_PRETRAINED_CONFIG_ARCHIVE_MAP

#     def __init__(self,
#                  vocab_size_or_config_json_file=30522,
#                  hidden_size=768,
#                  num_hidden_layers=12,
#                  num_attention_heads=12,
#                  intermediate_size=3072,
#                  hidden_act="gelu",
#                  hidden_dropout_prob=0.1,
#                  attention_probs_dropout_prob=0.1,
#                  max_position_embeddings=512,
#                  type_vocab_size=2,
#                  initializer_range=0.02,
#                  layer_norm_eps=1e-12,
#                  **kwargs):
#         super(BertConfig, self).__init__(**kwargs)
#         if isinstance(vocab_size_or_config_json_file, str) or (sys.version_info[0] == 2
#                         and isinstance(vocab_size_or_config_json_file, unicode)):
#             with open(vocab_size_or_config_json_file, "r", encoding='utf-8') as reader:
#                 json_config = json.loads(reader.read())
#             for key, value in json_config.items():
#                 self.__dict__[key] = value
#         elif isinstance(vocab_size_or_config_json_file, int):
#             self.vocab_size = vocab_size_or_config_json_file
#             self.hidden_size = hidden_size
#             self.num_hidden_layers = num_hidden_layers
#             self.num_attention_heads = num_attention_heads
#             self.hidden_act = hidden_act
#             self.intermediate_size = intermediate_size
#             self.hidden_dropout_prob = hidden_dropout_prob
#             self.attention_probs_dropout_prob = attention_probs_dropout_prob
#             self.max_position_embeddings = max_position_embeddings
#             self.type_vocab_size = type_vocab_size
#             self.initializer_range = initializer_range
#             self.layer_norm_eps = layer_norm_eps
#         else:
#             raise ValueError("First argument must be either a vocabulary size (int)"
#                              "or the path to a pretrained model config file (str)")

# class MlpModel(PreTrainedModel):
#     def __init__(self, config, model_input=1024):
#         super(MlpModel, self).__init__(config)
#         self.fc1=nn.Linear(model_input, model_input)
#         self.fc2=nn.Linear(model_input, model_input)
#         self.fc3=nn.Linear(model_input, model_input)
#         self.fc4=nn.Linear(model_input, 1)
#         self.dropout=nn.dropout(0.1)
#         self.relu=nn.ReLu()
#         self.mlp=nn.Sequential(
#             self.dropout,
#             self.fc1,
#             self.relu,
#             self.dropout,
#             self.fc2,
#             self.relu,
#             self.dropout,
#             self.fc3,
#             self.relu,
#             self.fc4
#         )
#     def forward(self, embedding_output):
#         encoder_outputs = self.encoder(embedding_output,
#                                        extended_attention_mask,
#                                        head_mask=head_mask)
#         sequence_output = encoder_outputs[0]
#         pooled_output = self.pooler(sequence_output)

#         outputs = (sequence_output, pooled_output,) + encoder_outputs[1:]  # add hidden_states and attentions if they are here
#         return outputs  # sequence_output, pooled_output, (hidden_states), (attentions)



# class BertModel(BertPreTrainedModel):
#     r"""
#     Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
#         **last_hidden_state**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, hidden_size)``
#             Sequence of hidden-states at the last layer of the model.
#         **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
#             list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
#             of shape ``(batch_size, sequence_length, hidden_size)``:
#             Hidden-states of the model at the output of each layer plus the initial embedding outputs.
#         **attentions**: (`optional`, returned when ``config.output_attentions=True``)
#             list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
#             Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

#     Examples::

#         >>> config = BertConfig.from_pretrained('bert-base-uncased')
#         >>> tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
#         >>> model = BertModel(config)
#         >>> input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
#         >>> outputs = model(input_ids)
#         >>> last_hidden_states = outputs[0]  # The last hidden-state is the first element of the output tuple

#     """
#     def __init__(self, config):
#         super(BertModel, self).__init__(config)

#         self.embeddings = BertEmbeddings(config)
#         self.encoder = BertEncoder(config)
#         self.pooler = BertPooler(config)

#         self.apply(self.init_weights)

#     def _resize_token_embeddings(self, new_num_tokens):
#         old_embeddings = self.embeddings.word_embeddings
#         new_embeddings = self._get_resized_embeddings(old_embeddings, new_num_tokens)
#         self.embeddings.word_embeddings = new_embeddings
#         return self.embeddings.word_embeddings

#     def _prune_heads(self, heads_to_prune):
#         """ Prunes heads of the model.
#             heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
#             See base class PreTrainedModel
#         """
#         for layer, heads in heads_to_prune.items():
#             self.encoder.layer[layer].attention.prune_heads(heads)

#     def forward(self, input_ids, token_type_ids=None, attention_mask=None, position_ids=None, head_mask=None):
#         if attention_mask is None:
#             attention_mask = torch.ones_like(input_ids)
#         if token_type_ids is None:
#             token_type_ids = torch.zeros_like(input_ids)

#         # We create a 3D attention mask from a 2D tensor mask.
#         # Sizes are [batch_size, 1, 1, to_seq_length]
#         # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
#         # this attention mask is more simple than the triangular masking of causal attention
#         # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
#         extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)

#         # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
#         # masked positions, this operation will create a tensor which is 0.0 for
#         # positions we want to attend and -10000.0 for masked positions.
#         # Since we are adding it to the raw scores before the softmax, this is
#         # effectively the same as removing these entirely.
#         extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
#         extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

#         # Prepare head mask if needed
#         # 1.0 in head_mask indicate we keep the head
#         # attention_probs has shape bsz x n_heads x N x N
#         # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
#         # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
#         if head_mask is not None:
#             if head_mask.dim() == 1:
#                 head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
#                 head_mask = head_mask.expand(self.config.num_hidden_layers, -1, -1, -1, -1)
#             elif head_mask.dim() == 2:
#                 head_mask = head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)  # We can specify head_mask for each layer
#             head_mask = head_mask.to(dtype=next(self.parameters()).dtype) # switch to fload if need + fp16 compatibility
#         else:
#             head_mask = [None] * self.config.num_hidden_layers

#         embedding_output = self.embeddings(input_ids, position_ids=position_ids, token_type_ids=token_type_ids)
#         encoder_outputs = self.encoder(embedding_output,
#                                        extended_attention_mask,
#                                        head_mask=head_mask)
#         sequence_output = encoder_outputs[0]
#         pooled_output = self.pooler(sequence_output)

#         outputs = (sequence_output, pooled_output,) + encoder_outputs[1:]  # add hidden_states and attentions if they are here
#         return outputs  # sequence_output, pooled_output, (hidden_states), (attentions)

