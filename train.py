import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
import tiktoken

import os
import sys
import pickle
import time
import argparse

import numpy as np
import matplotlib.pyplot as plt

import utils 
import model as miku # the naming scheme clashes with the torch naming scheme

def train_step(strokes, pen_lifts, text, style_vectors, model, alpha_set, bce, optimizer):
    device = next(model.parameters()).device
    strokes = strokes.to(device)
    pen_lifts = pen_lifts.to(device)
    text = text.to(device)
    style_vectors = style_vectors.to(device)

    alphas = utils.get_alphas(len(strokes), alpha_set)  # alpha_set is (60), alphas is (32, 1)
    alphas = alphas.view(-1, 1, 1)
    eps = torch.randn_like(strokes)
    x_perturbed = torch.sqrt(alphas) * strokes + torch.sqrt(1 - alphas) * eps

    model.train() # set model to training mode
    optimizer.zero_grad() # zero parameter grads

    score, pl_pred, att = model(x_perturbed, text, torch.sqrt(alphas), style_vectors) # forward
    loss = miku.loss_fn(eps, score, pen_lifts, pl_pred, alphas, bce)

    return loss, score, att

def train(train_loader, model, iterations, optimizer, scheduler, alpha_set, print_every=1, save_every=5000, device='mps'):
    s = time.time() # maybe use perf counter?
    bce = nn.BCELoss(reduction='none')
    train_loss = miku.AverageMeter()

    dataloader = iter(train_loader)

    for count in range(iterations):
        try:
            strokes, text, style_vectors = next(dataloader)
        except StopIteration:
            dataloader = iter(train_loader)
            strokes, text, style_vectors = next(dataloader)

        strokes, pen_lifts = strokes[:, :, :2], strokes[:, :, 2:]

        loss, score, att = train_step(strokes, pen_lifts, text, style_vectors, model, alpha_set, bce, optimizer)
        
        loss.backward()
        optimizer.step()
        scheduler.step()

        train_loss.update(loss.item())

        if (count + 1) % print_every == 0:
            print(f"Iteration {count + 1}, Loss {train_loss.avg:.6f}, Time {time.time() - s:.2f}s")
            train_loss.reset()

        if (count + 1) % save_every == 0:
            os.makedirs('./weights', exist_ok=True)
            save_path = f'./weights/model_step{count + 1}.pt'
            torch.save(model.state_dict(), save_path)

    # Save final model
    os.makedirs('./weights', exist_ok=True)
    torch.save(model.state_dict(), './weights/model.pt')

def main():
    parser = argparse.ArgumentParser()    
    parser.add_argument('--steps', help='number of trainsteps, default 60k', default=60000, type=int)
    parser.add_argument('--batchsize', help='default 96', default=32, type=int)
    parser.add_argument('--seqlen', help='sequence length during training, default 480', default=994, type=int)
    parser.add_argument('--textlen', help='text length during training, default 50', default=50, type=int)
    parser.add_argument('--width', help='offline image width, default 1400', default=1400, type=int)
    parser.add_argument('--warmup', help='number of warmup steps, default 10k', default=10000, type=int)
    parser.add_argument('--dropout', help='dropout rate, default 0', default=0.0, type=float)
    parser.add_argument('--num_attlayers', help='number of attentional layers at lowest resolution', default=2, type=int)
    parser.add_argument('--channels', help='number of channels in first layer, default 128', default=128, type=int)
    parser.add_argument('--print_every', help='show train loss every n iters', default=1000, type=int)
    parser.add_argument('--save_every', help='save ckpt every n iters', default=10000, type=int)

    args = parser.parse_args()
    NUM_STEPS = args.steps
    BATCH_SIZE = args.batchsize
    MAX_SEQ_LEN = args.seqlen
    MAX_TEXT_LEN = args.textlen
    IMG_WIDTH = args.width
    DROP_RATE = args.dropout
    NUM_ATTLAYERS = args.num_attlayers
    WARMUP_STEPS = args.warmup
    PRINT_EVERY = args.print_every
    SAVE_EVERY = args.save_every
    C1 = args.channels
    C2 = C1 * 3//2
    C3 = C1 * 2
    MAX_SEQ_LEN = MAX_SEQ_LEN - (MAX_SEQ_LEN%8) + 8
    BUFFER_SIZE = 3000
    L = 60
    
    path = './data/train_strokes.p'
    device = torch.device('cuda' if torch.cuda.is_available() else 'mps')
    tokenizer = tiktoken.get_encoding('cl100k_base') # using tiktoken instead of their default tokenizer
    beta_set = utils.get_beta_set()
    alpha_set = torch.cumprod(1 - beta_set, dim=0)

    print(f'using device {device}')

    style_extractor = miku.StyleExtractor().to(device)
    model = miku.DiffusionWriter(num_layers=NUM_ATTLAYERS, c1=C1, c2=C2, c3=C3, drop_rate=DROP_RATE)
    optimizer = optim.Adam(model.parameters(), lr=1e-3, betas=(0.9, 0.98), eps=1e-9)
    scheduler = miku.InvSqrtScheduler(optimizer, C3)

    strokes , text, samples = utils.preprocess_data(path, MAX_TEXT_LEN, MAX_SEQ_LEN, IMG_WIDTH, 96)
    train_loader = utils.build_dataset(strokes, text, samples, style_extractor, BATCH_SIZE, device)
    print('starting training...')

    train(train_loader, model, NUM_STEPS, optimizer, scheduler, alpha_set, PRINT_EVERY, SAVE_EVERY, device)

if __name__ == '__main__':
    main()

