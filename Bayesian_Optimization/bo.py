import pdb
import pickle
import json
import sys
import os
import time 
import os.path
import collections
import torch
from tqdm import tqdm
import itertools
from scipy.spatial.distance import pdist
import matplotlib.pyplot as plt
from sparse_gp import SparseGP
import scipy.stats as sps
import numpy as np
import scipy.io
from scipy.io import loadmat
from scipy.stats import pearsonr
sys.path.append('%s/../software/enas' % os.path.dirname(os.path.realpath(__file__))) 
sys.path.append('%s/..' % os.path.dirname(os.path.realpath(__file__))) 
sys.path.insert(0, '../')
from models.SVGe import SVGE
from utils import util
# from evaluate_BN import Eval_BN
from shutil import copy
from ConfigSpace.read_and_write import json as config_space_json_r_w

import argparse 

'''Experiment settings'''
parser = argparse.ArgumentParser(description='Bayesian optimization experiments.')
# must specify
parser.add_argument('--data-name', default='final_structures6', help='graph dataset name')
parser.add_argument('--save-appendix', default='../state_dicts/SVGE_ENAS/', 
                    help='what is appended to data-name as save-name for results')
parser.add_argument('--checkpoint', type=int, default=300, 
                    help="load which epoch's model checkpoint")
parser.add_argument('--res-dir', default='res/', 
                    help='where to save the Bayesian optimization results')
parser.add_argument("--device",                 type=str, default="cuda:1")

# BO settings
parser.add_argument('--predictor', action='store_true', default=False,
                    help='if True, use the performance predictor instead of SGP')
parser.add_argument('--grad-ascent', action='store_true', default=False,
                    help='if True and predictor=True, perform gradient-ascent with predictor')
parser.add_argument('--BO-rounds', type=int, default=10, 
                    help="how many rounds of BO to perform")
parser.add_argument('--BO-batch-size', type=int, default=5,#0, 
                    help="how many data points to select in each BO round")
parser.add_argument('--sample-dist', default='uniform', 
                    help='from which distrbiution to sample random points in the latent \
                    space as candidates to select; uniform or normal')
parser.add_argument('--random-baseline', action='store_true', default=False,
                    help='whether to include a baseline that randomly selects points \
                    to compare with Bayesian optimization')
parser.add_argument('--random-as-train', action='store_true', default=False, #######
                    help='if true, no longer use original train data to initialize SGP \
                    but randomly generates 1000 initial points as train data')
parser.add_argument('--random-as-test', action='store_true', default=False,
                    help='if true, randomly generates 100 points from the latent space \
                    as the additional testing data')
parser.add_argument('--vis-2d', action='store_true', default=False,
                    help='do visualization experiments on 2D space')

#Hyperparameter to be set with autoencoder
parser.add_argument('--data-type', default='ENAS',
                    help='ENAS: ENAS-format CNN structures')
parser.add_argument('--model', default='SVGE', help='model to use:  SVGE')
parser.add_argument('--hs', type=int, default=250, metavar='N',
                    help='hidden size of nodes')
parser.add_argument('--nz', type=int, default=56, metavar='N',
                    help='number of dimensions of latent vectors z')
parser.add_argument('--bidirectional', action='store_true', default=False,
                    help='whether to use bidirectional encoding')


args = parser.parse_args()
data_name = args.data_name
save_appendix = args.save_appendix
data_dir = os.path.join(os.getcwd(), '../datasets/ENAS/')
checkpoint = args.checkpoint
data_type = args.data_type
model_name = args.model
ndim,gdim = args.hs, args.nz
bidir = args.bidirectional
vis_2d = args.vis_2d
device = args.device

##############################################################################
#
#                              Runfolder
#
##############################################################################
#Create Log Directory
res_dir='{}_{}/{}/{}'.format(args.res_dir, model_name, args.data_type, time.strftime("%Y%m%d-%H%M%S"))

# '''Load hyperparameters'''
##############################################################################
#
#                           Dataset Config
#
##############################################################################

data_config_path ='../configs/data_configs/ENAS_configspace.json'
#Get Data specific configs
data_config = json.load(open(data_config_path, 'r'))

##############################################################################
#
#                           Model Config
#
##############################################################################
#Get Model configs
model_config_path='../configs/model_configs/svge_configspace.json'
model_configspace = config_space_json_r_w.read(open(model_config_path, 'r').read())
model_config = model_configspace.get_default_configuration().get_dictionary()

data_type=args.data_type
model_name=args.model
ndim=model_config['node_embedding_dim']
gdim=model_config['graph_embedding_dim']

                    

'''Load graph_args'''
with open(data_dir + data_name + '.pkl', 'rb') as f:
    _, _, graph_args = pickle.load(f)
START_TYPE, END_TYPE = graph_args.START_TYPE, graph_args.END_TYPE
max_n = graph_args.max_n
nvt = graph_args.num_vertex_type



'''BO settings'''
BO_rounds = args.BO_rounds
batch_size = args.BO_batch_size
sample_dist = args.sample_dist
random_baseline = args.random_baseline 
random_as_train = args.random_as_train
random_as_test = args.random_as_test

# other BO hyperparameters
lr = 0.0005  # the learning rate to train the SGP model
max_iter = 100  # how many iterations to optimize the SGP each time

# architecture performance evaluator
   
sys.path.append('%s/../software/enas/src/cifar10' % os.path.dirname(os.path.realpath(__file__))) 
from evaluation import *
eva = Eval_NN()  # build the network acc evaluater
                 # defined in ../software/enas/src/cifar10/evaluation.py

data = loadmat(save_appendix + '{}_latent_epoch{}.mat'.format(data_name, checkpoint))  # load train/test data
#data = loadmat(data_dir + '{}_latent.mat'.format(data_name))  # load train/test data

model_name=args.model
# do BO experiments with 10 random seeds
for rand_idx in range(1,11):


    save_dir = '{}/results_{}/'.format(res_dir, rand_idx)  # where to save the BO results

    if not os.path.exists(save_dir):
        os.makedirs(save_dir) 

    # backup files
    copy('bo.py', save_dir)
    copy('run_bo.sh', save_dir)

    # set seed
    random_seed = rand_idx
    torch.manual_seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    np.random.seed(random_seed)
    

    # load the model
    model=eval(model_name)(model_config=model_config, data_config=data_config)
    
    if args.predictor:
        predictor = nn.Sequential(
                nn.Linear(args.nz, args.hs), 
                nn.Tanh(), 
                nn.Linear(args.hs, 1)
                )
        model.predictor = predictor
    model.to(device)
    util.load_module_state(model, save_appendix + 'model_checkpoint{}.obj'.format(checkpoint))

    # load the data
    X_train = data['Z_train']
    y_train = -data['Y_train'].reshape((-1,1))
    

    if random_as_train:
        print("Generating random points from the latent space as initial train data")
        random_inputs = np.random.randn(1000, gdim) * X_train.std(0) + X_train.mean(0)
        random_inputs = torch.FloatTensor(random_inputs).to(device)
        valid_arcs_random = util.decode_from_latent_space(random_inputs, model, None, 500, max_n, False, data_type)
        print("Evaluating random points")
        random_scores = []
        max_random_score = -1e8
        for i in range(len(valid_arcs_random)):
            arc = valid_arcs_random[ i ] 
            if arc is not None:
                score = -eva.eval(arc)
                if score > max_random_score:
                    max_random_score = score
            else:
                score = None
            random_scores.append(score)
            print(i)
        # replace None scores with the worst score in y_train
        random_scores = [x if x is not None else max_random_score for x in random_scores]
        util.save_object(random_scores, "{}scores{}.dat".format(save_dir, -1))
        util.save_object(valid_arcs_random, "{}valid_arcs_final{}.dat".format(save_dir, -1))

        X_train = random_inputs.cpu().numpy()
        y_train = np.array(random_scores).reshape((-1, 1))
        util.save_object((X_train, y_train), save_dir+'train_random_X_y.dat')
        scipy.io.savemat(save_dir+'train_random_X_y.mat', 
                         mdict={
                             'X_train': X_train, 
                             'y_train': y_train, 
                             }
                         )
    
    print('amount of training data:', len(X_train))

    mean_y_train, std_y_train = np.mean(y_train), np.std(y_train)
    print('Mean, std of y_train is ', mean_y_train, std_y_train)
    y_train = (y_train - mean_y_train) / std_y_train
    X_test = data['Z_test']
    y_test = -data['Y_test'].reshape((-1,1))
    y_test = (y_test - mean_y_train) / std_y_train
    best_train_score = min(y_train)
    util.save_object((mean_y_train, std_y_train), "{}mean_std_y_train.dat".format(save_dir))

    print("Best train score is: ", best_train_score)
   
    

    if random_as_test:
        print("Generating random points from the latent space as testing data")
        random_inputs = np.random.randn(100, gdim) * X_train.std(0) + X_train.mean(0)
        random_inputs = torch.FloatTensor(random_inputs).to(device)
        valid_arcs_random = util.decode_from_latent_space(random_inputs, model, None, 500, max_n, False, data_type)

        print("Evaluating random points")
        random_scores = []
        for i in range(len(valid_arcs_random)):
            arc = valid_arcs_random[ i ] 
            if arc is not None:
                score = -eva.eval(arc)
                score = (score - mean_y_train) / std_y_train
            else:
                score = max(y_train)[ 0 ]

            random_scores.append(score)
            print(i)
        X_test2 = random_inputs.cpu().numpy()
        y_test2 = np.array(random_scores).reshape((-1, 1))
        util.save_object((X_test2, y_test2), save_dir+'random_X_y.dat')
        scipy.io.savemat(save_dir+'random_X_y.mat', 
                         mdict={
                             'X_random': X_test2, 
                             'y_random': y_test2, 
                             }
                         )
        print("Average pairwise distance between train points = {}".format(np.mean(pdist(X_train))))
        print("Average pairwise distance between test points = {}".format(np.mean(pdist(X_test2))))

    if vis_2d:
        print("Generating grid points from the 2-dim latent space to visualize smoothness w.r.t. score")
        #random_inputs = torch.randn(y_test.shape[0], nz).cuda()
        z0 = torch.zeros(1, args.nz).to(device)
        z0 = torch.zeros(1, args.nz).to(device)
        z0 = z0.detach()
        max_xy = 0.3
        #max_xy = 0.6
        x_range = np.arange(-max_xy, max_xy, 0.005)
        y_range = np.arange(max_xy, -max_xy, -0.005)
        n = len(x_range)
        x_range, y_range = np.meshgrid(x_range, y_range)
        x_range, y_range = x_range.reshape((-1, 1)), y_range.reshape((-1, 1))

        if True:  # select two principal components to visualize
            from sklearn.decomposition import PCA
            pca = PCA(n_components=2, whiten=True)
            pca.fit(X_train)
            d1, d2 = pca.components_[0:1], pca.components_[1:2]
            new_x_range = x_range * d1
            new_y_range = y_range * d2
            grid_inputs = torch.FloatTensor(new_x_range + new_y_range).to(device)
        else:
            grid_inputs = torch.FloatTensor(np.concatenate([x_range, y_range], 1)).to(device)
            if args.nz > 2:
                grid_inputs = torch.cat([grid_inputs, z0[:, 2:].expand(grid_inputs.shape[0], -1)], 1)

        valid_arcs_grid = []
        batch = 3000
        for i in range(0, grid_inputs.shape[0], batch):
            batch_grid_inputs = grid_inputs[i:i+batch, :]
            valid_arcs_grid += util.decode_from_latent_space(batch_grid_inputs, model, None,  100, max_n, False, data_type) ##100
        print("Evaluating 2D grid points")
        print("Total points: " + str(grid_inputs.shape[0]))
        grid_scores = []
        x, y = [], []
        for i in range(len(valid_arcs_grid)):
            arc = valid_arcs_grid[ i ] 
            if arc is not None:
                score = eva.eval(arc)
                x.append(x_range[i, 0])
                y.append(y_range[i, 0])
                grid_scores.append(score)
            else:
                score = 0
            #grid_scores.append(score)
            print(i)
        grid_inputs = grid_inputs.cpu().numpy()
        grid_y = np.array(grid_scores).reshape((n, n))
        util.save_object((grid_inputs, -grid_y), save_dir + 'grid_X_y.dat')
        util.save_object((x, y, grid_scores), save_dir + 'scatter_points.dat')
        if data_type == 'BN':
            vmin, vmax = -15000, -11000
        else:
            vmin, vmax = 0.7, 0.76
        ticks = np.linspace(vmin, vmax, 9, dtype=int).tolist()
        cmap = plt.cm.get_cmap('viridis')
        #f = plt.imshow(grid_y, cmap=cmap, interpolation='nearest')
        sc = plt.scatter(x, y, c=grid_scores, cmap=cmap, vmin=vmin, vmax=vmax, s=10)
        plt.colorbar(sc, ticks=ticks)
        plt.savefig(save_dir + "2D_vis.pdf")


    '''Bayesian optimiation begins here'''
    iteration = 0
    best_score = 1e15
    best_arc = None
    best_random_score = 1e15
    best_random_arc = None
    print("Average pairwise distance between train points = {}".format(np.mean(pdist(X_train))))
    print("Average pairwise distance between test points = {}".format(np.mean(pdist(X_test))))

    if os.path.exists(save_dir + 'Test_RMSE_ll.txt'):
        os.remove(save_dir + 'Test_RMSE_ll.txt')
    if os.path.exists(save_dir + 'best_arc_scores.txt'):
        os.remove(save_dir + 'best_arc_scores.txt')
    while iteration < BO_rounds:

        if args.predictor:
            pred = model.predictor(torch.FloatTensor(X_test).to(device))
            pred = pred.detach().cpu().numpy()
            pred = (-pred - mean_y_train) / std_y_train
            uncert = np.zeros_like(pred)
        else:
            # We fit the GP
            M = 500
            sgp = SparseGP(X_train, 0 * X_train, y_train, M)
            sgp.train_via_ADAM(X_train, 0 * X_train, y_train, X_test, X_test * 0,  \
                y_test, minibatch_size = 2 * M, max_iterations = max_iter, learning_rate = lr)
            pred, uncert = sgp.predict(X_test, 0 * X_test)

        print("predictions: ", pred.reshape(-1))
        print("real values: ", y_test.reshape(-1))
        error = np.sqrt(np.mean((pred - y_test)**2))
        testll = np.mean(sps.norm.logpdf(pred - y_test, scale = np.sqrt(uncert)))
        print('Test RMSE: ', error)
        print('Test ll: ', testll)
        pearson = float(pearsonr(pred.reshape(-1), y_test.reshape(-1))[0])
        print('Pearson r: ', pearson)
        with open(save_dir + 'Test_RMSE_ll.txt', 'a') as test_file:
            test_file.write('Test RMSE: {:.4f}, ll: {:.4f}, Pearson r: {:.4f}\n'.format(error, testll, pearson))

        error_if_predict_mean = np.sqrt(np.mean((np.mean(y_train, 0) - y_test)**2))
        print('Test RMSE if predict mean: ', error_if_predict_mean)
        if args.predictor:
            pred = model.predictor(torch.FloatTensor(X_train).to(device))
            pred = pred.detach().cpu().numpy()
            pred = (-pred - mean_y_train) / std_y_train
            uncert = np.zeros_like(pred)
        else:
            pred, uncert = sgp.predict(X_train, 0 * X_train)
        error = np.sqrt(np.mean((pred - y_train)**2))
        trainll = np.mean(sps.norm.logpdf(pred - y_train, scale = np.sqrt(uncert)))
        print('Train RMSE: ', error)
        print('Train ll: ', trainll)

        if random_as_test:
            if args.predictor:
                pred = model.predictor(torch.FloatTensor(X_test2).to(device))
                pred = pred.detach().cpu().numpy()
                pred = (-pred - mean_y_train) / std_y_train
                uncert = np.zeros_like(pred)
            else:
                pred, uncert = sgp.predict(X_test2, 0 * X_test2)
            error = np.sqrt(np.mean((pred - y_test2)**2))
            testll = np.mean(sps.norm.logpdf(pred - y_test2, scale = np.sqrt(uncert)))
            print('Random Test RMSE: ', error)
            print('Random Test ll: ', testll)
            pearson = float(pearsonr(pred.reshape(-1), y_test2.reshape(-1))[0])
            print('Pearson r: ', pearson)
            with open(save_dir + 'Random_Test_RMSE_ll.txt', 'a') as test_file:
                test_file.write('Random Test RMSE: {:.4f}, ll: {:.4f}, Pearson r: {:.4f}\n'.format(error, testll, pearson))


        # We pick the next batch of inputs
        if args.predictor:
            if random_as_train:
                if sample_dist == 'normal':
                    grid = X_train.mean(0) + np.random.randn(10000, gdim) * X_train.std(0)
                elif sample_dist == 'uniform':
                    grid = X_train.min(0) + np.random.rand(10000, gdim) * (X_train.max(0)-X_train.min(0))
            else:  # select random X_train
                train_idxs = np.random.permutation(range(len(X_train)))[:10000]
                grid = X_train[train_idxs]
            grid = torch.FloatTensor(grid).to(device)

            if not args.grad_ascent:
                pred = model.predictor(grid)
                pred = pred.detach().cpu().numpy()
                pred = (-pred - mean_y_train) / std_y_train
                selected_idxs = np.argsort(pred[:, 0])[:batch_size]
                next_inputs = grid[selected_idxs]
            else:
                grid.requires_grad=True
                ga_lr = 0.001  # learning rate of gradient ascent
                print('Performing gradient ascent...')
                ga_pbar = tqdm(range(10))
                for ga_iter in ga_pbar:
                    pred = model.predictor(grid)
                    ga_pbar.set_description('Max pred: {:.4f}, mean pred: {:.4f}, std pred: {:.4f}'.format(pred.max(), pred.mean(), pred.std()))
                    grads = torch.autograd.grad([x for x in pred], grid)[0]
                    grid = grid + ga_lr * grads
                pred = pred.detach().cpu().numpy()
                selected_idxs = np.argsort(-pred[:, 0])[:batch_size]
                next_inputs = grid[selected_idxs]
            next_inputs = next_inputs.detach().cpu().numpy()

        else:
            next_inputs = sgp.batched_greedy_ei(batch_size, np.min(X_train, 0), np.max(X_train, 0), np.mean(X_train, 0), np.std(X_train, 0), sample=sample_dist)
        valid_arcs_final = util.decode_from_latent_space(torch.FloatTensor(next_inputs).to(device), model, None, 500, max_n, False, data_type)

        if random_baseline:
            if args.sample_dist == 'uniform':
                random_inputs = np.random.rand(batch_size, gdim) * (X_train.max(0)-X_train.min(0)) + X_train.min(0)
            elif args.sample_dist == 'normal':
                random_inputs = np.random.randn(batch_size, gdim) * X_train.std(0) + X_train.mean(0)
            random_inputs = torch.FloatTensor(random_inputs).to(device)
            valid_arcs_random = util.decode_from_latent_space(random_inputs, model, None, 500, max_n, False, data_type)

        new_features = next_inputs
        print("Evaluating selected points")
        scores = []
        for i in range(len(valid_arcs_final)):
            arc = valid_arcs_final[ i ] 
            if arc is not None:
                score = -eva.eval(arc)
                score = (score - mean_y_train) / std_y_train
            else:
                score = max(y_train)[ 0 ]
            if score < best_score:
                best_score = score
                best_arc = arc
            scores.append(score)
            print(i)

        print("Iteration {}'s selected arcs' scores:".format(iteration))
        print(scores, np.mean(scores))
        util.save_object(scores, "{}scores{}.dat".format(save_dir, iteration))
        util.save_object(valid_arcs_final, "{}valid_arcs_final{}.dat".format(save_dir, iteration))

        if random_baseline:
            print("Evaluating random points")
            random_scores = []
            for i in range(len(valid_arcs_random)):
                arc = valid_arcs_random[ i ] 
                if arc is not None:
                    score = -eva.eval(arc)
                    score = (score - mean_y_train) / std_y_train
                else:
                    score = max(y_train)[ 0 ]
                if score < best_random_score:
                    best_random_score = score
                    best_random_arc = arc
                random_scores.append(score)
                print(i)

            print("Iteration {}'s selected arcs' scores:".format(iteration))
            print(scores, np.mean(scores))
            print("Iteration {}'s random arcs' scores:".format(iteration))
            print(random_scores, np.mean(random_scores))
            util.save_object(valid_arcs_random, "{}valid_arcs_random{}.dat".format(save_dir, iteration))
            util.save_object(random_scores, "{}random_scores{}.dat".format(save_dir, iteration))

        if len(new_features) > 0:
            X_train = np.concatenate([ X_train, new_features ], 0)
            y_train = np.concatenate([ y_train, np.array(scores)[ :, None ] ], 0)

        print("Current iteration {}'s best score: {}".format(iteration, - best_score * std_y_train - mean_y_train))
        if random_baseline:
            print("Current iteration {}'s best random score: {}".format(iteration, - best_random_score * std_y_train - mean_y_train))
        print("Best train score is: ", -best_train_score * std_y_train - mean_y_train)
        if best_arc is not None:
            print("Best architecture: ", best_arc)
            with open(save_dir + 'best_arc_scores.txt', 'a') as score_file:
                score_file.write(best_arc + ', {:.4f}\n'.format(-best_score * std_y_train - mean_y_train))
            row = [int(x) for x in best_arc.split()]
            g_best=util.parse_graph_to_nx(best_arc[0], best_arc[1], flat=True)
            util.plot_DAG(g_best, save_dir, 'best_arc_iter_{}'.format(iteration), data_type=data_type, pdf=True)

        iteration += 1
        print(iteration)

