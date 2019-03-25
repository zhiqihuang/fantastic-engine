import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
import argparse
import numpy as np
import json, time
import matplotlib.pyplot as plt

from trajectoryPlugin.plugin import API


class Net(nn.Module):
	def __init__(self):
		super(Net, self).__init__()
		self.conv1 = nn.Conv2d(1, 20, 5, 1)
		self.conv2 = nn.Conv2d(20, 50, 5, 1)
		self.fc1 = nn.Linear(4*4*50, 500)
		self.fc2 = nn.Linear(500, 10)

	def forward(self, x):
		x = F.relu(self.conv1(x))
		x = F.max_pool2d(x, 2, 2)
		x = F.relu(self.conv2(x))
		x = F.max_pool2d(x, 2, 2)
		x = x.view(-1, 4*4*50)
		x = F.relu(self.fc1(x))
		x = self.fc2(x)
		return F.log_softmax(x, dim=1)

def train_standard(model, device, optimizer, epoch, api, args):
	model.train()
	for batch_idx, (data, target, weight) in enumerate(api.train_loader):   
		data, target, weight = data.to(device), target.to(device), weight.to(device)
		optimizer.zero_grad()
		output = model(data)
		loss = api.loss_func(output, target, weight, 'mean')
		loss.backward()
		optimizer.step()
		if batch_idx % args.log_interval == 0:
			print('Train Standard Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
					epoch, batch_idx * len(data), len(api.train_loader.dataset),
					100. * batch_idx / len(api.train_loader), loss.item()))

def train_reweight(model, device, optimizer, epoch, api, args):
	model.train()
	for batch_idx, (data, target, weight) in enumerate(api.train_loader):
		data, target, weight = data.to(device), target.to(device), weight.to(device)
		optimizer.zero_grad()
		output = model(data)
		loss = api.loss_func(output, target, weight, 'mean')
		loss.backward()
		optimizer.step()
		if batch_idx % args.log_interval == 0:
			print('Train Reweight Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
					epoch, batch_idx * len(data), len(api.train_loader.dataset),
					100. * batch_idx / len(api.train_loader), loss.item()))

	# record trajectory
	api.createTrajectory(model)

	# cluster trajectory + reweight data
	if epoch > args.burn_in:
		api.clusterTrajectory() # run gmm cluster
		api.reweightData(model, 1e6) # update train_loader
    

def forward_fn(model, device, api, forward_type, test_loader=None):
	model.eval()
	loss = 0
	correct = 0
	if forward_type == 'test':
		with torch.no_grad():
			for data, target in test_loader:
				data, target = data.to(device), target.to(device)
				output = model(data)
				loss += api.loss_func(output, target, None, 'sum').item() # sum up batch loss
				pred = output.argmax(dim=1, keepdim=True) # get the index of the max log-probability
				correct += pred.eq(target.view_as(pred)).sum().item()
		
		loss /= len(test_loader.dataset)
		accuracy = 100. * correct / len(test_loader.dataset)
		print('Test Loss = {:.6f}, Test Accuracy = {:.2f}'.format(loss, accuracy))
	
	elif forward_type == 'train':
		with torch.no_grad():
			for batch_idx, (data, target, weight) in enumerate(api.train_loader): 
				data, target = data.to(device), target.to(device)
				output = model(data)
				loss += api.loss_func(output, target, None, 'sum').item() # sum up batch loss
				pred = output.argmax(dim=1, keepdim=True) # get the index of the max log-probability
				correct += pred.eq(target.view_as(pred)).sum().item()

		loss /= len(api.train_loader.dataset)
		accuracy = 100. * correct / len(api.train_loader.dataset)
		print('Train Loss = {:.6f}, Train Accuracy = {:.2f}'.format(loss, accuracy))
				
	elif forward_type == 'validation':
		with torch.no_grad():
			for batch_idx, (data, target) in enumerate(api.valid_loader): 
				data, target = data.to(device), target.to(device)
				output = model(data)
				loss += api.loss_func(output, target, None, 'sum').item() # sum up batch loss
				pred = output.argmax(dim=1, keepdim=True) # get the index of the max log-probability
				correct += pred.eq(target.view_as(pred)).sum().item()

		loss /= len(api.valid_loader.dataset)
		accuracy = 100. * correct / len(api.valid_loader.dataset)
		print('Validation Loss = {:.6f}, Validation Accuracy = {:.2f}'.format(loss, accuracy))

	return loss, accuracy

def main():
	# Training settings
	parser = argparse.ArgumentParser(description='MNIST Baseline Reweight Comparison')
	parser.add_argument('--batch_size', type=int, default=64, metavar='N',
						help='input batch size for training (default: 64)')
	parser.add_argument('--epochs', type=int, default=10, metavar='N',
						help='number of epochs to train (default: 10)')
	parser.add_argument('--burn_in', type=int, default=5, metavar='N',
						help='number of burn-in epochs (default: 5)')
	parser.add_argument('--valid_size', type=int, default=1000, metavar='N',
					help='input validation size (default: 1000)')
	parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
						help='learning rate (default: 0.01)')
	parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
						help='SGD momentum (default: 0.5)')
	parser.add_argument('--noise_level', type=float, default=0.1, metavar='M',
					help='percentage of noise data (default: 0.1)')
	parser.add_argument('--num_cluster', type=int, default=3, metavar='M',
					help='number of cluster (default: 3)')
	parser.add_argument('--seed', type=int, default=1, metavar='S',
						help='random seed (default: 1)')
	parser.add_argument('--log-interval', type=int, default=10, metavar='N',
						help='how many batches to wait before logging training status')
	parser.add_argument('--save-model', action='store_true', default=False,
						help='For Saving the current Model')
	
	args = parser.parse_args()

	if args.seed != 0:
		torch.manual_seed(args.seed)

	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

	trainset = datasets.MNIST('../data', train=True, download=True,
				 transform=transforms.Compose([
					 transforms.ToTensor(),
					 transforms.Normalize((0.1307,), (0.3081,))
				 ]))

	test_loader = torch.utils.data.DataLoader(
		datasets.MNIST('../data', train=False, transform=transforms.Compose([
						   transforms.ToTensor(),
						   transforms.Normalize((0.1307,), (0.3081,))
					   ])),
		batch_size=args.batch_size, shuffle=True)

	datalen = len(trainset)

	trainset, validset = torch.utils.data.random_split(trainset, [datalen - args.valid_size, args.valid_size])

	model_standard = Net().to(device)
	optimizer_standard = optim.SGD(model_standard.parameters(), lr=args.lr, momentum=args.momentum)

	model_reweight = Net().to(device)
	optimizer_reweight = optim.SGD(model_reweight.parameters(), lr=args.lr, momentum=args.momentum)
	
	standard_train_loss = []
	standard_train_accuracy = []
	standard_valid_loss = []
	standard_valid_accuracy = []
	standard_test_loss = []
	standard_test_accuracy = []

	reweight_train_loss = []
	reweight_train_accuracy = []
	reweight_valid_loss = []
	reweight_valid_accuracy = []
	reweight_test_loss = []
	reweight_test_accuracy = []

	api = API(num_cluster=args.num_cluster, device=device, iprint=1)
	api.dataLoader(trainset, validset, batch_size=args.batch_size)

	for epoch in range(1, args.epochs + 1):

		train_standard(model_standard, device, optimizer_standard, epoch, api, args)
		loss, accuracy = forward_fn(model_standard, device, api, 'train')
		standard_train_loss.append(loss)
		standard_train_accuracy.append(accuracy)
		
		loss, accuracy = forward_fn(model_standard, device, api, 'validation')
		standard_valid_loss.append(loss)
		standard_valid_accuracy.append(accuracy)

		loss, accuracy = forward_fn(model_standard, device, api, 'test', test_loader)
		standard_test_loss.append(loss)
		standard_test_accuracy.append(accuracy)

	api = API(num_cluster=args.num_cluster, device=device, iprint=1)
	api.dataLoader(trainset, validset, batch_size=args.batch_size)

	for epoch in range(1, args.epochs + 1):

		train_reweight(model_reweight, device, optimizer_reweight, epoch, api, args)
		loss, accuracy = forward_fn(model_reweight, device, api, 'train')
		reweight_train_loss.append(loss)
		reweight_train_accuracy.append(accuracy)
		
		loss, accuracy = forward_fn(model_reweight, device, api, 'validation')
		reweight_valid_loss.append(loss)
		reweight_valid_accuracy.append(accuracy)

		loss, accuracy = forward_fn(model_reweight, device, api, 'test', test_loader)
		reweight_test_loss.append(loss)
		reweight_test_accuracy.append(accuracy)

	if (args.save_model):
		torch.save(model.state_dict(),"mnist_cnn_baseline.pt")

	res = vars(args)

	res.update({'standard_train_loss':standard_train_loss})
	res.update({'standard_train_accuracy':standard_train_accuracy})
	res.update({'standard_valid_loss':standard_valid_loss})
	res.update({'standard_valid_accuracy':standard_valid_accuracy})
	res.update({'standard_test_loss':standard_test_loss})
	res.update({'standard_test_accuracy':standard_test_accuracy})

	res.update({'reweight_train_loss':reweight_train_loss})
	res.update({'reweight_train_accuracy':reweight_train_accuracy})
	res.update({'reweight_valid_loss':reweight_valid_loss})
	res.update({'reweight_valid_accuracy':reweight_valid_accuracy})
	res.update({'reweight_test_loss':reweight_test_loss})
	res.update({'reweight_test_accuracy':reweight_test_accuracy})
	
	res.update({'timestampe':int(time.time())})

	with open('mnist_cnn_baseline.data', 'a+') as f:
		f.write(json.dumps(res) + '\n')
	f.close()

if __name__ == '__main__':
	main()


# fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(nrows=2, ncols=3)

# x = range(1, epochs+1, 1)
# ax1.plot(x, train_loss, '--')
# ax1.title('Train Loss')
# ax2.plot(x, valid_loss, '--')
# ax3.plot(x, test_loss, '--')
# ax6.plot(x, test_accuracy, '--')

# plt.tight_layout()
# plt.show()





