from __future__ import print_function
from six.moves import range

import torch.backends.cudnn as cudnn
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
import torchvision.utils as vutils
from torch.nn.utils.rnn import *
import numpy as np
import os
import time
import cPickle as pickle
import string
import nltk
from PIL import Image, ImageFont, ImageDraw
from copy import deepcopy

from miscc.config import cfg
from miscc.utils import mkdir_p

from tensorboardX import summary
from tensorboardX import FileWriter

from model import G_NET, D_NET64, D_NET128, D_NET256, D_NET512, D_NET1024, INCEPTION_V3

from caption_gan_model import CaptionDiscriminator, CaptionGenerator




# ################## Shared functions ###################
def compute_mean_covariance(img):
    batch_size = img.size(0)
    channel_num = img.size(1)
    height = img.size(2)
    width = img.size(3)
    num_pixels = height * width

    # batch_size * channel_num * 1 * 1
    mu = img.mean(2, keepdim=True).mean(3, keepdim=True)

    # batch_size * channel_num * num_pixels
    img_hat = img - mu.expand_as(img)
    img_hat = img_hat.view(batch_size, channel_num, num_pixels)
    # batch_size * num_pixels * channel_num
    img_hat_transpose = img_hat.transpose(1, 2)
    # batch_size * channel_num * channel_num
    covariance = torch.bmm(img_hat, img_hat_transpose)
    covariance = covariance / num_pixels

    return mu, covariance


def KL_loss(mu, logvar):
    # -0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
    KLD = torch.mean(KLD_element).mul_(-0.5)
    return KLD


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        nn.init.orthogonal(m.weight.data, 1.0)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)
    elif classname.find('Linear') != -1:
        nn.init.orthogonal(m.weight.data, 1.0)
        if m.bias is not None:
            m.bias.data.fill_(0.0)


def load_params(model, new_param):
    for p, new_p in zip(model.parameters(), new_param):
        p.data.copy_(new_p)


def copy_G_params(model):
    flatten = deepcopy(list(p.data for p in model.parameters()))
    return flatten


def compute_inception_score(predictions, num_splits=1):
    # print('predictions', predictions.shape)
    scores = []
    for i in range(num_splits):
        istart = i * predictions.shape[0] // num_splits
        iend = (i + 1) * predictions.shape[0] // num_splits
        part = predictions[istart:iend, :]
        kl = part * \
            (np.log(part) - np.log(np.expand_dims(np.mean(part, 0), 0)))
        kl = np.mean(np.sum(kl, 1))
        scores.append(np.exp(kl))
    return np.mean(scores), np.std(scores)


def negative_log_posterior_probability(predictions, num_splits=1):
    # print('predictions', predictions.shape)
    scores = []
    for i in range(num_splits):
        istart = i * predictions.shape[0] // num_splits
        iend = (i + 1) * predictions.shape[0] // num_splits
        part = predictions[istart:iend, :]
        result = -1. * np.log(np.max(part, 1))
        result = np.mean(result)
        scores.append(result)
    return np.mean(scores), np.std(scores)


def load_network(gpus):
    netG = G_NET()
    netG.apply(weights_init)
    netG = torch.nn.DataParallel(netG, device_ids=gpus)
    print(netG)

    netsD = []
    if cfg.TREE.BRANCH_NUM > 0:
        netsD.append(D_NET64())
    if cfg.TREE.BRANCH_NUM > 1:
        netsD.append(D_NET128())
    if cfg.TREE.BRANCH_NUM > 2:
        netsD.append(D_NET256())
    if cfg.TREE.BRANCH_NUM > 3:
        netsD.append(D_NET512())
    if cfg.TREE.BRANCH_NUM > 4:
        netsD.append(D_NET1024())
    # TODO: if cfg.TREE.BRANCH_NUM > 5:

    for i in range(len(netsD)):
        netsD[i].apply(weights_init)
        netsD[i] = torch.nn.DataParallel(netsD[i], device_ids=gpus)
        # print(netsD[i])
    print('# of netsD', len(netsD))

    count = 0
    if cfg.TRAIN.NET_G != '':
        state_dict = torch.load(cfg.TRAIN.NET_G)
        netG.load_state_dict(state_dict)
        print('Load ', cfg.TRAIN.NET_G)

        istart = cfg.TRAIN.NET_G.rfind('_') + 1
        iend = cfg.TRAIN.NET_G.rfind('.')
        count = cfg.TRAIN.NET_G[istart:iend]
        count = int(count) + 1

    if cfg.TRAIN.NET_D != '':
        for i in range(len(netsD)):
            print('Load %s_%d.pth' % (cfg.TRAIN.NET_D, i))
            state_dict = torch.load('%s%d.pth' % (cfg.TRAIN.NET_D, i))
            netsD[i].load_state_dict(state_dict)

    inception_model = INCEPTION_V3()

    if cfg.CUDA:
        netG.cuda()
        for i in range(len(netsD)):
            netsD[i].cuda()
        inception_model = inception_model.cuda()
    inception_model.eval()

    return netG, netsD, len(netsD), inception_model, count


def define_optimizers(netG, netsD):
    optimizersD = []
    num_Ds = len(netsD)
    for i in range(num_Ds):
        opt = optim.Adam(netsD[i].parameters(),
                         lr=cfg.TRAIN.DISCRIMINATOR_LR,
                         betas=(0.5, 0.999))
        optimizersD.append(opt)

    # G_opt_paras = []
    # for p in netG.parameters():
    #     if p.requires_grad:
    #         G_opt_paras.append(p)
    optimizerG = optim.Adam(netG.parameters(),
                            lr=cfg.TRAIN.GENERATOR_LR,
                            betas=(0.5, 0.999))
    return optimizerG, optimizersD


def save_model(netG, avg_param_G, netsD, epoch, model_dir):
    load_params(netG, avg_param_G)
    torch.save(
        netG.state_dict(),
        '%s/netG_%d.pth' % (model_dir, epoch))
    for i in range(len(netsD)):
        netD = netsD[i]
        torch.save(
            netD.state_dict(),
            '%s/netD%d.pth' % (model_dir, i))
    print('Save G/Ds models.')


def save_img_results(imgs_tcpu, fake_imgs, num_imgs,
                     count, image_dir, summary_writer):
    num = cfg.TRAIN.VIS_COUNT

    # The range of real_img (i.e., self.imgs_tcpu[i][0:num])
    # is changed to [0, 1] by function vutils.save_image
    real_img = imgs_tcpu[-1][0:num]
    vutils.save_image(
        real_img, '%s/real_samples.png' % (image_dir),
        normalize=True)
    real_img_set = vutils.make_grid(real_img).numpy()
    real_img_set = np.transpose(real_img_set, (1, 2, 0))
    real_img_set = real_img_set * 255
    real_img_set = real_img_set.astype(np.uint8)
    sup_real_img = summary.image('real_img', real_img_set)
    summary_writer.add_summary(sup_real_img, count)

    for i in range(num_imgs):
        fake_img = fake_imgs[i][0:num]
        # The range of fake_img.data (i.e., self.fake_imgs[i][0:num])
        # is still [-1. 1]...
        vutils.save_image(
            fake_img.data, '%s/count_%09d_fake_samples%d.png' %
            (image_dir, count, i), normalize=True)

        fake_img_set = vutils.make_grid(fake_img.data).cpu().numpy()

        fake_img_set = np.transpose(fake_img_set, (1, 2, 0))
        fake_img_set = (fake_img_set + 1) * 255 / 2
        fake_img_set = fake_img_set.astype(np.uint8)

        sup_fake_img = summary.image('fake_img%d' % i, fake_img_set)
        summary_writer.add_summary(sup_fake_img, count)
        summary_writer.flush()


# ################## For uncondional tasks ######################### #
class GANTrainer(object):
    def __init__(self, output_dir, data_loader, imsize):
        if cfg.TRAIN.FLAG:
            self.model_dir = os.path.join(output_dir, 'Model')
            self.image_dir = os.path.join(output_dir, 'Image')
            self.log_dir = os.path.join(output_dir, 'Log')
            mkdir_p(self.model_dir)
            mkdir_p(self.image_dir)
            mkdir_p(self.log_dir)
            self.summary_writer = FileWriter(self.log_dir)

        s_gpus = cfg.GPU_ID.split(',')
        self.gpus = [int(ix) for ix in s_gpus]
        self.num_gpus = len(self.gpus)
        torch.cuda.set_device(self.gpus[0])
        cudnn.benchmark = True

        self.batch_size = cfg.TRAIN.BATCH_SIZE * self.num_gpus
        self.max_epoch = cfg.TRAIN.MAX_EPOCH
        self.snapshot_interval = cfg.TRAIN.SNAPSHOT_INTERVAL

        self.data_loader = data_loader
        self.num_batches = len(self.data_loader)


    def prepare_data(self, data):
        imgs = data

        vimgs = []
        for i in range(self.num_Ds):
            if cfg.CUDA:
                vimgs.append(Variable(imgs[i]).cuda())
            else:
                vimgs.append(Variable(imgs[i]))

        return imgs, vimgs

    def train_Dnet(self, idx, count):
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion = self.criterion

        netD, optD = self.netsD[idx], self.optimizersD[idx]
        real_imgs = self.real_imgs[idx]
        fake_imgs = self.fake_imgs[idx]
        real_labels = self.real_labels[:batch_size]
        fake_labels = self.fake_labels[:batch_size]
        #
        netD.zero_grad()
        #
        real_logits = netD(real_imgs)
        fake_logits = netD(fake_imgs.detach())
        #
        errD_real = criterion(real_logits[0], real_labels)
        errD_fake = criterion(fake_logits[0], fake_labels)
        #
        errD = errD_real + errD_fake
        errD.backward()
        # update parameters
        optD.step()
        # log
        if flag == 0:
            summary_D = summary.scalar('D_loss%d' % idx, errD.data[0])
            self.summary_writer.add_summary(summary_D, count)
        return errD

    def train_Gnet(self, count):
        self.netG.zero_grad()
        errG_total = 0
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion = self.criterion
        real_labels = self.real_labels[:batch_size]

        for i in range(self.num_Ds):
            netD = self.netsD[i]
            outputs = netD(self.fake_imgs[i])
            errG = criterion(outputs[0], real_labels)
            # errG = self.stage_coeff[i] * errG
            errG_total = errG_total + errG
            if flag == 0:
                summary_G = summary.scalar('G_loss%d' % i, errG.data[0])
                self.summary_writer.add_summary(summary_G, count)

        # Compute color preserve losses
        if cfg.TRAIN.COEFF.COLOR_LOSS > 0:
            if self.num_Ds > 1:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-1])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-2].detach())
                like_mu2 = cfg.TRAIN.COEFF.COLOR_LOSS * nn.MSELoss()(mu1, mu2)
                like_cov2 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    nn.MSELoss()(covariance1, covariance2)
                errG_total = errG_total + like_mu2 + like_cov2
            if self.num_Ds > 2:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-2])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-3].detach())
                like_mu1 = cfg.TRAIN.COEFF.COLOR_LOSS * nn.MSELoss()(mu1, mu2)
                like_cov1 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    nn.MSELoss()(covariance1, covariance2)
                errG_total = errG_total + like_mu1 + like_cov1

            if flag == 0:
                sum_mu = summary.scalar('G_like_mu2', like_mu2.data[0])
                self.summary_writer.add_summary(sum_mu, count)
                sum_cov = summary.scalar('G_like_cov2', like_cov2.data[0])
                self.summary_writer.add_summary(sum_cov, count)
                if self.num_Ds > 2:
                    sum_mu = summary.scalar('G_like_mu1', like_mu1.data[0])
                    self.summary_writer.add_summary(sum_mu, count)
                    sum_cov = summary.scalar('G_like_cov1', like_cov1.data[0])
                    self.summary_writer.add_summary(sum_cov, count)

        errG_total.backward()
        self.optimizerG.step()
        return errG_total

    def train(self):

        self.netG, self.netsD, self.num_Ds,\
            self.inception_model, start_count = load_network(self.gpus)
        avg_param_G = copy_G_params(self.netG)

        self.optimizerG, self.optimizersD = \
            define_optimizers(self.netG, self.netsD)

        self.criterion = nn.BCELoss()

        self.real_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(1))
        self.fake_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(0))
        nz = cfg.GAN.Z_DIM
        noise = Variable(torch.FloatTensor(self.batch_size, nz))
        fixed_noise = \
            Variable(torch.FloatTensor(self.batch_size, nz).normal_(0, 1))

        if cfg.CUDA:
            self.criterion.cuda()
            noise, fixed_noise = noise.cuda(), fixed_noise.cuda()
            self.real_labels = self.real_labels.cuda()
            self.fake_labels = self.fake_labels.cuda()

        predictions = []
        count = start_count
        start_epoch = start_count // (self.num_batches)
        for epoch in range(start_epoch, self.max_epoch):
            start_t = time.time()

            for step, data in enumerate(self.data_loader, 0):
                #######################################################
                # (0) Prepare training data
                ######################################################
                self.imgs_tcpu, self.real_imgs = self.prepare_data(data)

                #######################################################
                # (1) Generate fake images
                ######################################################
                noise.data.normal_(0, 1)
                self.fake_imgs, _, _ = self.netG(noise)

                #######################################################
                # (2) Update D network
                ######################################################
                errD_total = 0
                for i in range(self.num_Ds):
                    errD = self.train_Dnet(i, count)
                    errD_total += errD

                #######################################################
                # (3) Update G network: maximize log(D(G(z)))
                ######################################################
                errG_total = self.train_Gnet(count)
                for p, avg_p in zip(self.netG.parameters(), avg_param_G):
                    avg_p.mul_(0.999).add_(0.001, p.data)

                # for inception score
                pred = self.inception_model(self.fake_imgs[-1].detach())
                predictions.append(pred.data.cpu().numpy())

                if count % 100 == 0:
                    summary_D = summary.scalar('D_loss', errD_total.data[0])
                    summary_G = summary.scalar('G_loss', errG_total.data[0])
                    self.summary_writer.add_summary(summary_D, count)
                    self.summary_writer.add_summary(summary_G, count)
                if step == 0:
                    print('''[%d/%d][%d/%d] Loss_D: %.2f Loss_G: %.2f'''
                           % (epoch, self.max_epoch, step, self.num_batches,
                              errD_total.data[0], errG_total.data[0]))
                count = count + 1

                if count % cfg.TRAIN.SNAPSHOT_INTERVAL == 0:
                    save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)
                    save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)
                    # Save images
                    backup_para = copy_G_params(self.netG)
                    load_params(self.netG, avg_param_G)
                    #
                    self.fake_imgs, _, _ = self.netG(fixed_noise)
                    save_img_results(self.imgs_tcpu, self.fake_imgs, self.num_Ds,
                                    count, self.image_dir, self.summary_writer)
                    #
                    load_params(self.netG, backup_para)

                    # Compute inception score
                    if len(predictions) > 500:
                        predictions = np.concatenate(predictions, 0)
                        mean, std = compute_inception_score(predictions, 10)
                        # print('mean:', mean, 'std', std)
                        m_incep = summary.scalar('Inception_mean', mean)
                        self.summary_writer.add_summary(m_incep, count)
                        #
                        mean_nlpp, std_nlpp = \
                            negative_log_posterior_probability(predictions, 10)
                        m_nlpp = summary.scalar('NLPP_mean', mean_nlpp)
                        self.summary_writer.add_summary(m_nlpp, count)
                        #
                        predictions = []

            end_t = time.time()
            print('Total Time: %.2fsec' % (end_t - start_t))

        save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)
        save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)

        self.summary_writer.close()

    def save_superimages(self, images, folder, startID, imsize):
        fullpath = '%s/%d_%d.png' % (folder, startID, imsize)
        vutils.save_image(images.data, fullpath, normalize=True)

    def save_singleimages(self, images, folder, startID, imsize):
        for i in range(images.size(0)):
            fullpath = '%s/%d_%d.png' % (folder, startID + i, imsize)
            # range from [-1, 1] to [0, 1]
            img = (images[i] + 1.0) / 2
            img = images[i].add(1).div(2).mul(255).clamp(0, 255).byte()
            # range from [0, 1] to [0, 255]
            ndarr = img.permute(1, 2, 0).data.cpu().numpy()
            im = Image.fromarray(ndarr)
            im.save(fullpath)

    def evaluate(self, split_dir):
        if cfg.TRAIN.NET_G == '':
            print('Error: the path for morels is not found!')
        else:
            # Build and load the generator
            netG = G_NET()
            netG.apply(weights_init)
            netG = torch.nn.DataParallel(netG, device_ids=self.gpus)
            print(netG)
            # state_dict = torch.load(cfg.TRAIN.NET_G)
            state_dict = \
                torch.load(cfg.TRAIN.NET_G,
                           map_location=lambda storage, loc: storage)
            netG.load_state_dict(state_dict)
            print('Load ', cfg.TRAIN.NET_G)

            # the path to save generated images
            s_tmp = cfg.TRAIN.NET_G
            istart = s_tmp.rfind('_') + 1
            iend = s_tmp.rfind('.')
            iteration = int(s_tmp[istart:iend])
            s_tmp = s_tmp[:s_tmp.rfind('/')]
            save_dir = '%s/iteration%d/%s' % (s_tmp, iteration, split_dir)
            if cfg.TEST.B_EXAMPLE:
                folder = '%s/super' % (save_dir)
            else:
                folder = '%s/single' % (save_dir)
            print('Make a new folder: ', folder)
            mkdir_p(folder)

            nz = cfg.GAN.Z_DIM
            noise = Variable(torch.FloatTensor(self.batch_size, nz))
            if cfg.CUDA:
                netG.cuda()
                noise = noise.cuda()

            # switch to evaluate mode
            netG.eval()
            num_batches = int(cfg.TEST.SAMPLE_NUM / self.batch_size)
            cnt = 0
            for step in xrange(num_batches):
                noise.data.normal_(0, 1)
                fake_imgs, _, _ = netG(noise)
                if cfg.TEST.B_EXAMPLE:
                    self.save_superimages(fake_imgs[-1], folder, cnt, 256)
                else:
                    self.save_singleimages(fake_imgs[-1], folder, cnt, 256)
                    # self.save_singleimages(fake_imgs[-2], folder, 128)
                    # self.save_singleimages(fake_imgs[-3], folder, 64)
                cnt += self.batch_size


# ################# Text to image task############################ #
class condGANTrainer(object):
    def __init__(self, output_dir, data_loader, imsize):
        if cfg.TRAIN.FLAG:
            self.model_dir = os.path.join(output_dir, 'Model')
            self.image_dir = os.path.join(output_dir, 'Image')
            self.log_dir = os.path.join(output_dir, 'Log')
            mkdir_p(self.model_dir)
            mkdir_p(self.image_dir)
            mkdir_p(self.log_dir)
            self.summary_writer = FileWriter(self.log_dir)

        s_gpus = cfg.GPU_ID.split(',')
        self.gpus = [int(ix) for ix in s_gpus]
        self.num_gpus = len(self.gpus)
        torch.cuda.set_device(self.gpus[0])
        cudnn.benchmark = True

        self.total_cyclic_loss = []

        self.batch_size = cfg.TRAIN.BATCH_SIZE * self.num_gpus
        self.max_epoch = cfg.TRAIN.MAX_EPOCH
        self.snapshot_interval = cfg.TRAIN.SNAPSHOT_INTERVAL

        self.data_loader = data_loader
        self.num_batches = len(self.data_loader)

    def prepare_data(self, data):
        imgs, w_imgs, t_embedding, key, captions = data

        real_vimgs, wrong_vimgs = [], []
        if cfg.CUDA:
            vembedding = Variable(t_embedding).cuda()
        else:
            vembedding = Variable(t_embedding)
        for i in range(self.num_Ds):
            if cfg.CUDA:
                real_vimgs.append(Variable(imgs[i]).cuda())
                wrong_vimgs.append(Variable(w_imgs[i]).cuda())
            else:
                real_vimgs.append(Variable(imgs[i]))
                wrong_vimgs.append(Variable(w_imgs[i]))
        return imgs, real_vimgs, wrong_vimgs, vembedding, captions

    def train_Dnet(self, idx, count):
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion, mu = self.criterion, self.mu

        netD, optD = self.netsD[idx], self.optimizersD[idx]
        real_imgs = self.real_imgs[idx]
        wrong_imgs = self.wrong_imgs[idx]
        fake_imgs = self.fake_imgs[idx]
        #
        netD.zero_grad()
        # Forward
        real_labels = self.real_labels[:batch_size]
        fake_labels = self.fake_labels[:batch_size]
        # for real
        real_logits = netD(real_imgs, mu.detach())
        wrong_logits = netD(wrong_imgs, mu.detach())
        fake_logits = netD(fake_imgs.detach(), mu.detach())
        #
        errD_real = criterion(real_logits[0], real_labels)
        errD_wrong = criterion(wrong_logits[0], fake_labels)
        errD_fake = criterion(fake_logits[0], fake_labels)
        if len(real_logits) > 1 and cfg.TRAIN.COEFF.UNCOND_LOSS > 0:
            errD_real_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(real_logits[1], real_labels)
            errD_wrong_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(wrong_logits[1], real_labels)
            errD_fake_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(fake_logits[1], fake_labels)
            #
            errD_real = errD_real + errD_real_uncond
            errD_wrong = errD_wrong + errD_wrong_uncond
            errD_fake = errD_fake + errD_fake_uncond
            #
            errD = errD_real + errD_wrong + errD_fake
        else:
            errD = errD_real + 0.5 * (errD_wrong + errD_fake)
        # backward
        errD.backward()
        # update parameters
        optD.step()
        # log
        if flag == 0:
            summary_D = summary.scalar('D_loss%d' % idx, errD.data[0])
            self.summary_writer.add_summary(summary_D, count)
        return errD

    def train_Gnet(self, count):
        #print("Caption")
        #print(self.captions[0])
        # print("Caption Details --> ")
        # print("Type --> ", type(self.captions))
        # print("Length --> ", len(self.captions))

        # print("Individual Caption detail -->")
        # print("Type --> ", type(self.captions[0]))
        # print("Length --> ", [len(a) for a in self.captions])
        captions_generated=[]
        length_generated=[]
        caption_max_generated = []
        for idx, captiontuple in enumerate(self.captions):
            # print("Processing record number -->", idx)
            captionlist = list(captiontuple)
            captionlength = []
            for i, txt in enumerate(captionlist):
                # print("Processing Caption -->", i)
                # preprocess txt and wrong_txt
                txt = str(txt)
                txt = txt.strip()
                txt = txt.encode('ascii', 'ignore')
                txt = txt.decode('ascii')
                exclude = set(string.punctuation)
                preproc_txt = ''.join(ch for ch in txt if ch not in exclude)
                tokens = nltk.tokenize.word_tokenize(preproc_txt.lower())
                caption = []
                caption.append(self.vocab('<start>'))
                caption.extend([self.vocab(token) for token in tokens])
                caption.append(self.vocab('<end>'))
                #caption = torch.LongTensor(caption)
                if torch.cuda.is_available():
                    # print("CUDAA!!")
                    caption = torch.cuda.LongTensor(caption)
                else:
                    caption = torch.LongTensor(caption)
                captionlength.append(len(caption))
                captionlist[i] = caption
                # print("Processed Caption -->", i)
            #captiontensor = torch.stack(captionlist)
            captions_generated.append(captionlist)
            length_generated.append(captionlength)
            # print("Generating Tensor")
            #captionlistmax = torch.zeros(len(captionlist), max(captionlength)).long()
            if torch.cuda.is_available():
                captionlistmax = torch.cuda.LongTensor(len(captionlist), max(captionlength)).fill_(0)
            else:
                captionlistmax = torch.zeros(len(captionlist), max(captionlength)).long()
            # print("Entering for loop")
            for i, cap in enumerate(captionlist):
                # print("Processing Max length for Caption -->", i)
                end = captionlength[i]
                captionlistmax[i, :end] = cap[:end]
            #captionlistmaxtensor = torch.stack(captionlistmax)
            caption_max_generated.append(captionlistmax)

        # print("caption max generated -->")
        # print("Size 0 -->", caption_max_generated[0].size())
        # print("Size 1 -->", caption_max_generated[1].size())

        # print("length_generated -->")
        # print("length 0 --> ", length_generated[0])
        # print("lenth 1 --> ", length_generated[1])
        self.netG.zero_grad()
        errG_total = 0
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion, mu, logvar = self.criterion, self.mu, self.logvar
        real_labels = self.real_labels[:batch_size]
        for i in range(self.num_Ds):
            outputs = self.netsD[i](self.fake_imgs[i], mu)
            errG = criterion(outputs[0], real_labels)
            if len(outputs) > 1 and cfg.TRAIN.COEFF.UNCOND_LOSS > 0:
                errG_patch = cfg.TRAIN.COEFF.UNCOND_LOSS *\
                    criterion(outputs[1], real_labels)
                errG = errG + errG_patch
            errG_total = errG_total + errG
            if flag == 0:
                summary_D = summary.scalar('G_loss%d' % i, errG.data[0])
                self.summary_writer.add_summary(summary_D, count)

        # Compute color consistency losses
        if cfg.TRAIN.COEFF.COLOR_LOSS > 0:
            if self.num_Ds > 1:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-1])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-2].detach())
                like_mu2 = cfg.TRAIN.COEFF.COLOR_LOSS * nn.MSELoss()(mu1, mu2)
                like_cov2 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    nn.MSELoss()(covariance1, covariance2)
                errG_total = errG_total + like_mu2 + like_cov2
                if flag == 0:
                    sum_mu = summary.scalar('G_like_mu2', like_mu2.data[0])
                    self.summary_writer.add_summary(sum_mu, count)
                    sum_cov = summary.scalar('G_like_cov2', like_cov2.data[0])
                    self.summary_writer.add_summary(sum_cov, count)
            if self.num_Ds > 2:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-2])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-3].detach())
                like_mu1 = cfg.TRAIN.COEFF.COLOR_LOSS * nn.MSELoss()(mu1, mu2)
                like_cov1 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    nn.MSELoss()(covariance1, covariance2)

                errG_total = errG_total + like_mu1 + like_cov1
                if flag == 0:
                    sum_mu = summary.scalar('G_like_mu1', like_mu1.data[0])
                    self.summary_writer.add_summary(sum_mu, count)
                    sum_cov = summary.scalar('G_like_cov1', like_cov1.data[0])
                    self.summary_writer.add_summary(sum_cov, count)

        # ak6384 - Modification start

        # Generate caption with caption GAN (inverse GAN)
        # fake_images.requires_grad = False # freeze the caption generator
        # cycle gan params
        lambda_a = 2
        lambda_b = 2
        mle_criterion = nn.CrossEntropyLoss()
        self.caption_generator.zero_grad()
        loss_cycle = []
        for index in range(len(caption_max_generated)):
            itrlist = [caption_max_generated[index], self.fake_imgs[-2], length_generated[index]]
            ziplist = zip(*itrlist)
            # print("Zipped")
            # print(type(ziplist[0][2]))
            result = sorted(ziplist, key=lambda x: int(x[2]), reverse=True)
            inputimages = []
            inputcaptions = []
            inputlengths = []
            # print("processing sorted")
            for observation in result:
                inputcaptions.append(observation[0])
                inputimages.append(observation[1])
                inputlengths.append(observation[2])
            inputimages = torch.stack(inputimages)
            inputcaptions = torch.stack(inputcaptions)
            # print("Processed sorted")
            sampled_captions, _ = self.caption_generator.forward(inputimages, inputcaptions, inputlengths)
            targets = pack_padded_sequence(inputcaptions, inputlengths, batch_first=True)[0]
            loss_cycle.append(mle_criterion(sampled_captions, targets) * lambda_a)
        # print("Loss Cycle")
        # print(loss_cycle[0].size())
        # print(loss_cycle)
        loss_cycle_A = torch.stack(loss_cycle).mean(0)
        # print("Average Loss Cycle")
        # print(loss_cycle_A)
        self.total_cyclic_loss.append(loss_cycle_A)
        loss_cycle_A.backward()
        self.optim_captionG.step()


        # ak6384 - Modification end


        kl_loss = KL_loss(mu, logvar) * cfg.TRAIN.COEFF.KL
        errG_total = errG_total + kl_loss
        errG_total.backward()
        self.optimizerG.step()




        # self.cycle_a_losses.append(loss_cycle_A.data[0] + errG_total.data[0])
        return kl_loss, errG_total, loss_cycle_A

    def train(self):

        # ak6384 - Modification start

        if cfg.DATASET_NAME == 'birds':
            with open('../data/birds_vocab.pkl', 'rb') as f:
                self.vocab = pickle.load(f)
        elif cfg.DATASET_NAME == 'flowers':
            with open('../data/flowers_vocab.pkl', 'rb') as f:
                self.vocab = pickle.load(f)
        else:
            print('Dataset not supported, please select either birds or flowers.')
            exit()

        self.figure_path = "../figures/"
        self.embed_size = 256
        self.hidden_size = 512
        self.num_layers = 1
        self.caption_generator = CaptionGenerator(self.embed_size, self.hidden_size, len(self.vocab),
                                                  self.num_layers).cuda()
        self.caption_discriminator = CaptionDiscriminator(self.embed_size, self.hidden_size, len(self.vocab),
                                                          self.num_layers).cuda()

        pretrained_caption_gen = '../checkpoints/pretrained-generator-100.pkl'
        pretrained_caption_disc = '../checkpoints/pretrained-discriminator-20.pkl'

        if os.path.exists(pretrained_caption_gen):
            print('loaded pretrained caption generator')
            self.caption_generator.load_state_dict(torch.load(pretrained_caption_gen))

        if os.path.exists(pretrained_caption_disc):
            print('loaded pretrained caption discriminator')
            self.caption_discriminator.load_state_dict(torch.load(pretrained_caption_disc))

        self.optim_captionG = torch.optim.Adam(list(self.caption_generator.parameters()))
        self.optim_captionD = torch.optim.Adam(list(self.caption_discriminator.parameters()))

        # ak6384 - Modification End

        self.netG, self.netsD, self.num_Ds,\
            self.inception_model, start_count = load_network(self.gpus)
        avg_param_G = copy_G_params(self.netG)

        self.optimizerG, self.optimizersD = \
            define_optimizers(self.netG, self.netsD)

        self.criterion = nn.BCELoss()

        self.real_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(1))
        self.fake_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(0))

        self.gradient_one = torch.FloatTensor([1.0])
        self.gradient_half = torch.FloatTensor([0.5])

        nz = cfg.GAN.Z_DIM
        noise = Variable(torch.FloatTensor(self.batch_size, nz))
        fixed_noise = \
            Variable(torch.FloatTensor(self.batch_size, nz).normal_(0, 1))

        if cfg.CUDA:
            self.criterion.cuda()
            self.real_labels = self.real_labels.cuda()
            self.fake_labels = self.fake_labels.cuda()
            self.gradient_one = self.gradient_one.cuda()
            self.gradient_half = self.gradient_half.cuda()
            noise, fixed_noise = noise.cuda(), fixed_noise.cuda()

        predictions = []
        count = start_count
        start_epoch = start_count // (self.num_batches)
        self.cycle_a_losses = []
        for epoch in range(start_epoch, self.max_epoch):
            start_t = time.time()
            # print("Epoch --> ", epoch)
            for step, data in enumerate(self.data_loader, 0):
                #######################################################
                # (0) Prepare training data
                ######################################################
                self.imgs_tcpu, self.real_imgs, self.wrong_imgs, \
                    self.txt_embedding, self.captions = self.prepare_data(data)
                # print("Step 1")
                #######################################################
                # (1) Generate fake images
                ######################################################
                noise.data.normal_(0, 1)
                self.fake_imgs, self.mu, self.logvar = \
                    self.netG(noise, self.txt_embedding)

                # ak6384 - Modification start
                for image in self.fake_imgs:
                    print(image.size())
                # ak6384 - Modification end
                # print("Step 2")
                #######################################################
                # (2) Update D network
                ######################################################
                errD_total = 0
                for i in range(self.num_Ds):
                    errD = self.train_Dnet(i, count)
                    errD_total += errD
                # print("Step 3")
                #######################################################
                # (3) Update G network: maximize log(D(G(z)))
                ######################################################
                kl_loss, errG_total, loss_cycle_A = self.train_Gnet(count)
                for p, avg_p in zip(self.netG.parameters(), avg_param_G):
                    avg_p.mul_(0.999).add_(0.001, p.data)

                # for inception score
                pred = self.inception_model(self.fake_imgs[-1].detach())
                predictions.append(pred.data.cpu().numpy())

                if count % 100 == 0:
                    summary_D = summary.scalar('D_loss', errD_total.data[0])
                    summary_G = summary.scalar('G_loss', errG_total.data[0])
                    summary_KL = summary.scalar('KL_loss', kl_loss.data[0])
                    summary_LC = summary.scalar('Cycle_loss', loss_cycle_A.data[0])
                    self.summary_writer.add_summary(summary_D, count)
                    self.summary_writer.add_summary(summary_G, count)
                    self.summary_writer.add_summary(summary_KL, count)
                    self.summary_writer.add_summary(summary_LC, count)

                count = count + 1

                if count % cfg.TRAIN.SNAPSHOT_INTERVAL == 0:
                    save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)
                    # Save images
                    backup_para = copy_G_params(self.netG)
                    load_params(self.netG, avg_param_G)
                    #
                    self.fake_imgs, _, _ = \
                        self.netG(fixed_noise, self.txt_embedding)
                    save_img_results(self.imgs_tcpu, self.fake_imgs, self.num_Ds,
                                     count, self.image_dir, self.summary_writer)
                    #
                    load_params(self.netG, backup_para)

                    # Compute inception score
                    if len(predictions) > 500:
                        predictions = np.concatenate(predictions, 0)
                        mean, std = compute_inception_score(predictions, 10)
                        # print('mean:', mean, 'std', std)
                        m_incep = summary.scalar('Inception_mean', mean)
                        self.summary_writer.add_summary(m_incep, count)
                        #
                        mean_nlpp, std_nlpp = \
                            negative_log_posterior_probability(predictions, 10)
                        m_nlpp = summary.scalar('NLPP_mean', mean_nlpp)
                        self.summary_writer.add_summary(m_nlpp, count)
                        #
                        predictions = []

            end_t = time.time()
            print('''[%d/%d][%d]
                         Loss_D: %.2f Loss_G: %.2f Loss_KL: %.2f Loss_Cycle: %.2f Time: %.2fs
                      '''  # D(real): %.4f D(wrong):%.4f  D(fake) %.4f
                  % (epoch, self.max_epoch, self.num_batches,
                     errD_total.data[0], errG_total.data[0],
                     kl_loss.data[0], loss_cycle_A.data[0], end_t - start_t))

            #Plot the graphs for Cyclic Loss for every 10 epochs
            if epoch%10==0:
                plt.plot(self.total_cyclic_loss, label='total_cycle_losses')
                plt.savefig(self.figure_path + 'total_cyclic_losses'+epoch+'.png')
                plt.clf()


        save_model(self.netG, avg_param_G, self.netsD, count, self.model_dir)
        self.summary_writer.close()

    def save_superimages(self, images_list, filenames,
                         save_dir, split_dir, imsize):
        batch_size = images_list[0].size(0)
        num_sentences = len(images_list)
        for i in range(batch_size):
            s_tmp = '%s/super/%s/%s' %\
                (save_dir, split_dir, filenames[i])
            folder = s_tmp[:s_tmp.rfind('/')]
            if not os.path.isdir(folder):
                print('Make a new folder: ', folder)
                mkdir_p(folder)
            #
            savename = '%s_%d.png' % (s_tmp, imsize)
            super_img = []
            for j in range(num_sentences):
                img = images_list[j][i]
                # print(img.size())
                img = img.view(1, 3, imsize, imsize)
                # print(img.size())
                super_img.append(img)
                # break
            super_img = torch.cat(super_img, 0)
            vutils.save_image(super_img, savename, nrow=10, normalize=True)

    def save_singleimages(self, images, filenames,
                          save_dir, split_dir, sentenceID, imsize):
        for i in range(images.size(0)):
            s_tmp = '%s/single_samples/%s/%s' %\
                (save_dir, split_dir, filenames[i])
            folder = s_tmp[:s_tmp.rfind('/')]
            if not os.path.isdir(folder):
                print('Make a new folder: ', folder)
                mkdir_p(folder)

            fullpath = '%s_%d_sentence%d.png' % (s_tmp, imsize, sentenceID)
            # range from [-1, 1] to [0, 255]
            img = images[i].add(1).div(2).mul(255).clamp(0, 255).byte()
            ndarr = img.permute(1, 2, 0).data.cpu().numpy()
            im = Image.fromarray(ndarr)
            im.save(fullpath)

    def evaluate(self, split_dir):
        if cfg.TRAIN.NET_G == '':
            print('Error: the path for morels is not found!')
        else:
            # Build and load the generator
            if split_dir == 'test':
                split_dir = 'valid'
            netG = G_NET()
            netG.apply(weights_init)
            netG = torch.nn.DataParallel(netG, device_ids=self.gpus)
            print(netG)
            # state_dict = torch.load(cfg.TRAIN.NET_G)
            state_dict = \
                torch.load(cfg.TRAIN.NET_G,
                           map_location=lambda storage, loc: storage)
            netG.load_state_dict(state_dict)
            print('Load ', cfg.TRAIN.NET_G)

            # the path to save generated images
            s_tmp = cfg.TRAIN.NET_G
            istart = s_tmp.rfind('_') + 1
            iend = s_tmp.rfind('.')
            iteration = int(s_tmp[istart:iend])
            s_tmp = s_tmp[:s_tmp.rfind('/')]
            save_dir = '%s/iteration%d' % (s_tmp, iteration)

            nz = cfg.GAN.Z_DIM
            noise = Variable(torch.FloatTensor(self.batch_size, nz))
            if cfg.CUDA:
                netG.cuda()
                noise = noise.cuda()

            # switch to evaluate mode
            netG.eval()
            for step, data in enumerate(self.data_loader, 0):
                imgs, t_embeddings, filenames = data
                if cfg.CUDA:
                    t_embeddings = Variable(t_embeddings).cuda()
                else:
                    t_embeddings = Variable(t_embeddings)
                # print(t_embeddings[:, 0, :], t_embeddings.size(1))

                embedding_dim = t_embeddings.size(1)
                batch_size = imgs[0].size(0)
                noise.data.resize_(batch_size, nz)
                noise.data.normal_(0, 1)

                fake_img_list = []
                for i in range(embedding_dim):
                    fake_imgs, _, _ = netG(noise, t_embeddings[:, i, :])
                    if cfg.TEST.B_EXAMPLE:
                        # fake_img_list.append(fake_imgs[0].data.cpu())
                        # fake_img_list.append(fake_imgs[1].data.cpu())
                        fake_img_list.append(fake_imgs[2].data.cpu())
                    else:
                        self.save_singleimages(fake_imgs[-1], filenames,
                                               save_dir, split_dir, i, 256)
                        # self.save_singleimages(fake_imgs[-2], filenames,
                        #                        save_dir, split_dir, i, 128)
                        # self.save_singleimages(fake_imgs[-3], filenames,
                        #                        save_dir, split_dir, i, 64)
                    # break
                if cfg.TEST.B_EXAMPLE:
                    # self.save_superimages(fake_img_list, filenames,
                    #                       save_dir, split_dir, 64)
                    # self.save_superimages(fake_img_list, filenames,
                    #                       save_dir, split_dir, 128)
                    self.save_superimages(fake_img_list, filenames,
                                          save_dir, split_dir, 256)
