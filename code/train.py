import os
import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torchvision
import torchvision.transforms as transforms

from utils import train, evaluate
from plots import plot_learning_curves, plot_confusion_matrix, plot_roc, plot_pr
from dataset import CheXpertDataSet
from models import DenseNet121
from scipy.special import softmax
from sklearn.metrics import roc_auc_score

cudnn.benchmark = True

torch.manual_seed(0)
if torch.cuda.is_available():
    torch.cuda.manual_seed(0)

PATH_DIR = '../data'
PATH_TRAIN = '../data/CheXpert-v1.0-small/data_train.csv'
PATH_VALID = '../data/CheXpert-v1.0-small/data_valid.csv'
PATH_TEST = '../data/CheXpert-v1.0-small/data_test.csv'
PATH_OUTPUT = "../output/"
os.makedirs(PATH_OUTPUT, exist_ok=True)
MODEL_OUTPUT = 'model.pth.tar'

NUM_EPOCHS = 6
BATCH_SIZE = 32 # 32 is max for 224x224, 16 is max for 320x320, 280x280
USE_CUDA = True  # Set 'True' if you want to use GPU
NUM_WORKERS = 8
num_labels = 14

# Data loading
print('===> Loading entire datasets')
normalize = transforms.Normalize([0.485, 0.456, 0.406],
                                 [0.229, 0.224, 0.225])

transformseq=transforms.Compose([
                                    #transforms.Resize(size=(320, 320)),
                                    # transforms.Resize(256),#smaller edge
                                    transforms.Resize(224),
                                    #transforms.RandomResizedCrop(224),
                                    transforms.CenterCrop(224),
                                    #transforms.CenterCrop(280),
                                    #transforms.CenterCrop(320), # padding
                                    transforms.RandomHorizontalFlip(),
                                    transforms.ToTensor(),
                                    normalize
                                ])

train_dataset = CheXpertDataSet(data_dir=PATH_DIR, image_list_file=PATH_TRAIN, transform = transformseq)
valid_dataset = CheXpertDataSet(data_dir=PATH_DIR, image_list_file=PATH_VALID, transform = transformseq)
test_dataset = CheXpertDataSet(data_dir=PATH_DIR, image_list_file=PATH_TEST, transform = transformseq)

# find weights
train_labels = np.array(train_dataset.labels)
num_all = len(train_labels)
weights = np.zeros((num_labels,3))
for i in range(np.shape(train_labels)[1]):
    count_negative = np.sum(train_labels[:,i] == 0)
    count_positive = np.sum(train_labels[:,i] == 1) # count positive(include uncertain) in i th label
    count_uncertain = np.sum(train_labels[:,i] == 2)

    # weights, make the micro average = 1
    weight_negative = num_all/3/count_negative if count_negative > 0 else 0
    weight_positive = num_all/3/count_positive if count_positive > 0 else 0
    weight_uncertain = num_all/3/count_uncertain if count_uncertain > 0 else 0

    weights[i] = [weight_negative, weight_positive, weight_uncertain]

weights = torch.FloatTensor(weights)
print('Weights')
print(weights)

# train shuffle=True
train_loader = DataLoader(dataset=train_dataset, batch_size=BATCH_SIZE, shuffle=True, num_workers=NUM_WORKERS, pin_memory=True)
valid_loader = DataLoader(dataset=valid_dataset, batch_size=BATCH_SIZE, shuffle=False, num_workers=NUM_WORKERS, pin_memory=True)
test_loader = DataLoader(dataset=test_dataset, batch_size=BATCH_SIZE, shuffle=False, num_workers=NUM_WORKERS, pin_memory=True)
print('Data Loaded')

model = DenseNet121(num_labels)
# mean of nn.CrossEntropyLoss() on each label, where nn.CrossEntropyLoss() include softmax & cross entropy, it is faster and stabler than cross entropy
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=1e-4)
scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.1)


if torch.cuda.device_count() > 1:
    print("Use", torch.cuda.device_count(), "GPUs")
    model = nn.DataParallel(model)

device = torch.device("cuda" if torch.cuda.is_available() and USE_CUDA else "cpu")
model.to(device)
criterion.to(device)

PATH_MODEL = os.path.join(PATH_OUTPUT, "MyCNN.pth")
if os.path.isfile(PATH_MODEL):
    model = torch.load(PATH_MODEL)
    print('Saved model loaded')

# train
best_val_loss = 1000000
train_losses = []
valid_losses = []
for epoch in range(NUM_EPOCHS):
    #scheduler.step() # no decay in the first step
    print('Learning rate in epoch:', epoch)
    for param_group in optimizer.param_groups:
        print(param_group['lr'])
    train_loss = train(model, device, train_loader, criterion, weights, optimizer, epoch)
    valid_loss, valid_results = evaluate(model, device, valid_loader, criterion, weights)
    train_losses.append(train_loss)
    valid_losses.append(valid_loss)

    is_best = valid_loss < best_val_loss  # let's keep the model that has the best loss, but you can also use another metric.
    if is_best:
        best_val_loss = valid_loss
        torch.save(model, os.path.join(PATH_OUTPUT, "MyCNN.pth"))

print('Training finished, model saved')

# save data of learning curves
df_learning = pd.DataFrame(data = {'Train Loss':train_losses,'Valid Loss':valid_losses} )
df_learning.index.name = 'Epoch'
df_learning.to_csv(os.path.join(PATH_OUTPUT,'LearningCurves.csv'))

# plot learning curves
plot_learning_curves(train_losses, valid_losses)#, train_accuracies, valid_accuracies)

# load best model
best_model = torch.load(os.path.join(PATH_OUTPUT, "MyCNN.pth"))
#test_loss, test_results = evaluate(best_model, device, test_loader, criterion)

# plot confusion matrix 
class_names = ['Negative', 'Positive', 'Uncertain']
label_names = [ 'No Finding', 'Enlarged Cardiomediastinum', 'Cardiomegaly', 'Lung Opacity', 'Lung Lesion', 'Edema', 'Consolidation',
                'Pneumonia', 'Atelectasis', 'Pneumothorax', 'Pleural Effusion', 'Pleural Other', 'Fracture', 'Support Devices']
#for i, label_name in enumerate(label_names): # i th observation
#    plot_confusion_matrix(test_results, class_names, i, label_name)


# convert output to positive probability
def predict_positive(model, device, data_loader):
    model.eval()
    # return a List of probabilities
    #input, target = zip(*data_loader)

    probas = np.array([])
    targets = np.array([])
    with torch.no_grad():
        for i, (input, target) in enumerate(data_loader):
            if isinstance(input, tuple):
                input = tuple([e.to(device) if type(e) == torch.Tensor else e for e in input])
            else:
                input = input.to(device)
            target = target.detach().to('cpu').numpy()
            targets = np.concatenate((targets, target), axis=0) if len(targets) > 0 else target
            #target = target.to(device)

            output = model(input) # num_batch x 14 x 3
            y_pred = output.detach().to('cpu').numpy()
            y_pred = y_pred[:,:,:2] # drop uncertain
            y_pred = softmax(y_pred, axis = -1)
            y_pred = y_pred[:,:,1] # keep positive only

            probas = np.concatenate((probas, y_pred), axis=0) if len(probas) > 0 else y_pred
    
    return targets, probas

test_targets, test_probs = predict_positive(best_model, device, test_loader)
plot_roc(test_targets, test_probs, label_names)
plot_pr(test_targets, test_probs, label_names)

