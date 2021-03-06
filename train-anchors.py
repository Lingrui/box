#!/usr/bin/env python3
import os
import sys
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'models/research/slim'))
sys.path.insert(0, 'picpac/build/lib.linux-x86_64-3.5')
import time
import datetime
import logging
from tqdm import tqdm
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
from nets import nets_factory, resnet_utils 
import picpac

class ShapeConfig:
    def __init__ (self, params=3, priors=1):
        self.params = params    # number of shape parameters
        self.priors = priors    # number of priors
        pass

    def predict_logits (self, ft):
        return slim.conv2d(ft, 2 * self.priors, 3, 1, activation_fn=None) 

    # these are just box deltas
    def predict_params (self, ft):
        return slim.conv2d(ft, self.params * self.priors, 3, 1, activation_fn=None)

    def params_loss (self, params, gt_params):
        diff = params - gt_params
        diff = diff * diff
        return tf.reduce_sum(diff, axis=1)
    pass

def create_net (ft, gt_anchors, gt_anchors_weight, gt_params, gt_params_weight, config):
    # ft:           B * H' * W' * 3     input feature, H' W' is feature map size
    # gt_counts:    B                   number of boxes in each sample of the batch
    # gt_boxes:     ? * 4               boxes
    with tf.variable_scope('anchor_net'):

        logits = config.predict_logits(ft)     # B * H' * W' * (M * 2)
        logits2 = tf.reshape(logits, (-1, 2))   # ? * 2

        gt_anchors = tf.reshape(gt_anchors, (-1, ))
        gt_anchors_weight = tf.reshape(gt_anchors_weight, (-1,))
        xe = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits2, labels=gt_anchors)
        xe = xe * gt_anchors_weight
        xe = tf.reduce_sum(xe) / (tf.reduce_sum(gt_anchors_weight) + 1)

        params = config.predict_params(ft)       # B * H' * W' * M * 4
        params2 = tf.reshape(params, (-1, config.params))     # ? * 4
        gt_params = tf.reshape(gt_params, (-1, config.params))
        gt_params_weight = tf.reshape(gt_params_weight, (-1,))
        pl = config.params_loss(params2, gt_params)
        pl = pl * gt_params_weight
        pl = tf.reduce_sum(pl) / (tf.reduce_sum(gt_params_weight) + 1)

    logits= tf.identity(logits, name='logits')
    params = tf.identity(params, name='params')
    xe = tf.identity(xe, name='xe') # cross-entropy
    pl = tf.identity(pl * FLAGS.pl_weight, name='pl') # params-loss
    reg = tf.identity(tf.reduce_sum(tf.losses.get_regularization_losses()) * FLAGS.re_weight, name='re')

    loss = tf.identity(xe + pl + reg, name='lo')

    return logits, params, loss, [loss, xe, pl, reg]

def patch_arg_scopes ():
    def resnet_arg_scope (weight_decay=0.0001):
        print_red("Patching resnet_v2 arg_scope when training from scratch")
        return resnet_utils.resnet_arg_scope(weight_decay=weight_decay,
                    batch_norm_decay=0.9,
                    batch_norm_epsilon=5e-4,
                    batch_norm_scale=False)
    nets_factory.arg_scopes_map['resnet_v1_50'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v1_101'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v1_152'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v1_200'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v2_50'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v2_101'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v2_152'] = resnet_arg_scope
    nets_factory.arg_scopes_map['resnet_v2_200'] = resnet_arg_scope
    pass

augments = None
#from . config import *
#if os.path.exists('config.py'):
def print_red (txt):
    print('\033[91m' + txt + '\033[0m')

def print_green (txt):
    print('\033[92m' + txt + '\033[0m')

print(augments)

flags = tf.app.flags
FLAGS = flags.FLAGS

flags.DEFINE_string('db', None, 'training db')
flags.DEFINE_string('val_db', None, 'validation db')
flags.DEFINE_integer('classes', 2, 'number of classes')
flags.DEFINE_string('mixin', None, 'mix-in training db')

flags.DEFINE_integer('size', None, '') 
flags.DEFINE_integer('batch', 1, 'Batch size.  ')
flags.DEFINE_integer('shift', 0, '')
flags.DEFINE_integer('backbone_stride', 16, '')
flags.DEFINE_integer('ft_filters', 256, '')
flags.DEFINE_integer('ft_stride', 4, '')

flags.DEFINE_string('backbone', 'resnet_v2_50', 'architecture')
flags.DEFINE_string('model', None, 'model directory')
flags.DEFINE_string('resume', None, 'resume training from this model')
flags.DEFINE_string('finetune', None, '')
flags.DEFINE_integer('max_to_keep', 100, '')

# optimizer settings
flags.DEFINE_float('lr', 0.01, 'Initial learning rate.')
flags.DEFINE_float('decay_rate', 0.95, '')
flags.DEFINE_float('decay_steps', 500, '')
flags.DEFINE_float('weight_decay', 0.00004, '')
#
flags.DEFINE_integer('epoch_steps', None, '')
flags.DEFINE_integer('max_epochs', 20000, '')
flags.DEFINE_integer('ckpt_epochs', 10, '')
flags.DEFINE_integer('val_epochs', 10, '')
flags.DEFINE_boolean('adam', False, '')

flags.DEFINE_float('pl_weight', 1.0/50, '')
flags.DEFINE_float('re_weight', 0.1, '')

flags.DEFINE_string('shape', 'circle', '')

COLORSPACE = 'BGR'
PIXEL_MEANS = tf.constant([[[[127.0, 127.0, 127.0]]]])
VGG_PIXEL_MEANS = tf.constant([[[[103.94, 116.78, 123.68]]]])

def setup_finetune (ckpt, exclusions):
    print("Finetuning %s" % ckpt)
    # TODO(sguada) variables.filter_variables()
    variables_to_restore = []
    for var in slim.get_model_variables():
        excluded = False
        for exclusion in exclusions:
            if var.op.name.startswith(exclusion):
                print("Excluding %s" % var.op.name)
                excluded = True
                break
        if not excluded:
            variables_to_restore.append(var)

    if tf.gfile.IsDirectory(ckpt):
        ckpt = tf.train.latest_checkpoint(ckpt)

    variables_to_train = []
    for scope in exclusions:
        variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
        variables_to_train.extend(variables)

    print("Training %d out of %d variables" % (len(variables_to_train), len(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES))))
    if len(variables_to_train) < 10:
        for var in variables_to_train:
            print("    %s" % var.op.name)

    return slim.assign_from_checkpoint_fn(
            ckpt, variables_to_restore,
            ignore_missing_vars=False), variables_to_train


def create_picpac_stream (db_path, is_training):
    assert os.path.exists(db_path)
    augments = []
    if is_training:
        augments = [
                  #{"type": "augment.flip", "horizontal": True, "vertical": False},
                  {"type": "augment.rotate", "min":-10, "max":10},
                  {"type": "augment.scale", "min":0.9, "max":1.1},
                  {"type": "augment.add", "range":20},
                ]
    else:
        augments = []

    picpac_config = {"db": db_path,
              "loop": is_training,
              "shuffle": is_training,
              "reshuffle": is_training,
              "annotate": True,
              "channels": 3,
              "stratify": is_training,
              "dtype": "float32",
              "batch": FLAGS.batch,
              "colorspace": COLORSPACE,
              "transforms": augments + [
                  {"type": "clip", "round": FLAGS.backbone_stride},
                  {"type": "anchors.dense." + FLAGS.shape, 'downsize': FLAGS.ft_stride},
                  {"type": "rasterize"},
                  ]
             }
    if is_training and not FLAGS.mixin is None:
        print("mixin support is incomplete in new picpac.")
    #    assert os.path.exists(FLAGS.mixin)
    #    picpac_config['mixin'] = FLAGS.mixin
    #    picpac_config['mixin_group_delta'] = 1
    #    pass
    return picpac.ImageStream(picpac_config)

def main (_):
    global PIXEL_MEANS

    logging.basicConfig(filename='train-%s-%s.log' % (FLAGS.backbone, datetime.datetime.now().strftime('%Y%m%d-%H%M%S')),level=logging.DEBUG, format='%(asctime)s %(message)s')

    if FLAGS.model:
        try:
            os.makedirs(FLAGS.model)
        except:
            pass

    if FLAGS.finetune:
        print_red("finetune, using RGB with vgg pixel means")
        COLORSPACE = 'RGB'
        PIXEL_MEANS = VGG_PIXEL_MEANS

    if FLAGS.shape == 'circle':
        shape_config = ShapeConfig(3)
    elif FLAGS.shape == 'box':
        shape_config = ShapeConfig(4)

    X = tf.placeholder(tf.float32, shape=(None, None, None, 3), name="images")
    # ground truth labels
    gt_anchors = tf.placeholder(tf.int32, shape=(None, None, None, shape_config.priors))
    gt_anchors_weight = tf.placeholder(tf.float32, shape=(None, None, None, shape_config.priors))
    gt_params = tf.placeholder(tf.float32, shape=(None, None, None, shape_config.priors * shape_config.params))
    gt_params_weight = tf.placeholder(tf.float32, shape=(None, None, None, shape_config.priors))

    is_training = tf.placeholder(tf.bool, name="is_training")

    if not FLAGS.finetune:
        patch_arg_scopes()
    #with \
    #     slim.arg_scope([slim.batch_norm], decay=0.9, epsilon=5e-4): 

    if not FLAGS.finetune:
        patch_arg_scopes()
    #with \
    #     slim.arg_scope([slim.batch_norm], decay=0.9, epsilon=5e-4): 
    network_fn = nets_factory.get_network_fn(FLAGS.backbone, num_classes=None,
                weight_decay=FLAGS.weight_decay, is_training=is_training)

    with slim.arg_scope([slim.conv2d, slim.conv2d_transpose, slim.max_pool2d], padding='SAME'), \
         slim.arg_scope([slim.conv2d, slim.conv2d_transpose], weights_regularizer=slim.l2_regularizer(2.5e-4), normalizer_fn=slim.batch_norm, normalizer_params={'decay': 0.9, 'epsilon': 5e-4, 'scale': False, 'is_training':is_training}), \
         slim.arg_scope([slim.batch_norm], is_training=is_training):
        bb, _ = network_fn(X-PIXEL_MEANS, global_pool=False, output_stride=16)
        assert FLAGS.backbone_stride % FLAGS.ft_stride == 0
        ss = FLAGS.backbone_stride // FLAGS.ft_stride
        ft = slim.conv2d_transpose(bb, FLAGS.ft_filters, ss*2, ss)
        _, _, loss, metrics = create_net(ft, gt_anchors, gt_anchors_weight, gt_params, gt_params_weight, shape_config)

    #network_fn = nets_factory.get_network_fn(FLAGS.backbone, num_classes=None,
    #            weight_decay=FLAGS.weight_decay, is_training=is_training)
    #ft, _ = network_fn(X, global_pool=False, output_stride=16)
    #logits = slim.conv2d_transpose(ft, FLAGS.classes, 32, 16)
    #logits = tf.identity(logits, name='logits')

    # probability of class 1 -- not very useful if FLAGS.classes > 2
    #probs = tf.squeeze(tf.slice(tf.nn.softmax(logits), [0,0,0,1], [-1,-1,-1,1]), 3)

    metric_names = [x.name[:-2] for x in metrics]

    def format_metrics (avg):
        return ' '.join(['%s=%.3f' % (a, b) for a, b in zip(metric_names, list(avg))])

    init_finetune, variables_to_train = None, None
    if FLAGS.finetune:
        print_red("finetune, using RGB with vgg pixel means")
        init_finetune, variables_to_train = setup_finetune(FLAGS.finetune, [FLAGS.net + '/logits'])

    global_step = tf.train.create_global_step()
    LR = tf.train.exponential_decay(FLAGS.lr, global_step, FLAGS.decay_steps, FLAGS.decay_rate, staircase=True)
    if FLAGS.adam:
        print("Using Adam optimizer, reducing LR by 100x")
        optimizer = tf.train.AdamOptimizer(LR/100)
    else:
        optimizer = tf.train.MomentumOptimizer(learning_rate=LR, momentum=0.9)

    train_op = slim.learning.create_train_op(loss, optimizer, global_step=global_step, variables_to_train=variables_to_train)
    saver = tf.train.Saver(max_to_keep=FLAGS.max_to_keep)

    stream = create_picpac_stream(FLAGS.db, True)
    # load validation db
    val_stream = None
    if FLAGS.val_db:
        val_stream = create_picpac_stream(FLAGS.val_db, False)

    epoch_steps = FLAGS.epoch_steps
    if epoch_steps is None:
        epoch_steps = (stream.size() + FLAGS.batch-1) // FLAGS.batch
    best = 0

    ss_config = tf.ConfigProto()
    ss_config.gpu_options.allow_growth=True
    with tf.Session(config=ss_config) as sess:
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        if init_finetune:
            init_finetune(sess)
        if FLAGS.resume:
            saver.restore(sess, FLAGS.resume)

        global_start_time = time.time()
        epoch = 0
        step = 0
        while epoch < FLAGS.max_epochs:
            start_time = time.time()
            cnt, metrics_sum = 0, np.array([0] * len(metrics), dtype=np.float32)
            progress = tqdm(range(epoch_steps), leave=False)
            for _ in progress:
                _, images, _, gt_anchors_, gt_anchors_weight_, gt_params_, gt_params_weight_ = stream.next()
                feed_dict = {X: images,
                             gt_anchors: gt_anchors_,
                             gt_anchors_weight: gt_anchors_weight_,
                             gt_params: gt_params_,
                             gt_params_weight: gt_params_weight_,
                             is_training: True}
                mm, _ = sess.run([metrics, train_op], feed_dict=feed_dict)
                metrics_sum += np.array(mm) * images.shape[0]
                cnt += images.shape[0]
                metrics_txt = format_metrics(metrics_sum/cnt)
                progress.set_description(metrics_txt)
                step += 1
                pass
            stop = time.time()
            msg = 'train epoch=%d step=%d ' % (epoch, step)
            msg += metrics_txt
            msg += ' elapsed=%.3f time=%.3f ' % (stop - global_start_time, stop - start_time)
            print_green(msg)
            logging.info(msg)

            epoch += 1

            if (epoch % FLAGS.val_epochs == 0) and val_stream:
                lr = sess.run(LR)
                # evaluation
                Ys, Ps = [], []
                cnt, metrics_sum = 0, np.array([0] * len(metrics), dtype=np.float32)
                val_stream.reset()
                progress = tqdm(val_stream, leave=False)
                for _, images, _, gt_anchors_, gt_anchors_weight_, gt_params_, gt_params_weight_ in progress:
                    feed_dict = {X: images,
                             gt_anchors: gt_anchors_,
                             gt_anchors_weight: gt_anchors_weight_,
                             gt_params: gt_params_,
                             gt_params_weight: gt_params_weight_,
                             is_training: False}
                    p, mm = sess.run([probs, metrics], feed_dict=feed_dict)
                    metrics_sum += np.array(mm) * images.shape[0]
                    cnt += images.shape[0]
                    Ys.extend(list(meta.labels))
                    Ps.extend(list(p))
                    metrics_txt = format_metrics(metrics_sum/cnt)
                    progress.set_description(metrics_txt)
                    pass
                assert cnt == val_stream.size()
                avg = metrics_sum / cnt
                if avg[0] > best:
                    best = avg[0]
                msg = 'valid epoch=%d step=%d ' % (epoch-1, step)
                msg += metrics_txt
                msg += ' lr=%.4f best=%.3f' % (lr, best)
                print_red(msg)
                logging.info(msg)
                #log.write('%d\t%s\t%.4f\n' % (epoch, '\t'.join(['%.4f' % x for x in avg]), best))
            # model saving
            if (epoch % FLAGS.ckpt_epochs == 0) and FLAGS.model:
                ckpt_path = '%s/%d' % (FLAGS.model, epoch)
                saver.save(sess, ckpt_path)
                print('saved to %s.' % ckpt_path)
            pass
        pass
    pass

if __name__ == '__main__':
    try:
        tf.app.run()
    except KeyboardInterrupt:
        pass

