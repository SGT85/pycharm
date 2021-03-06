
from matplotlib import pyplot
import numpy as np
import os
import shutil
from IPython import display

from caffe2.python import core, model_helper, net_drawer, workspace, visualize, brew

# If you would like to see some really detailed initializations,
# you can change --caffe2_log_level=0 to --caffe2_log_level=-1
core.GlobalInit(['caffe2', '--caffe2_log_level=0'])

# This section preps your image and test set in a leveldb
current_folder = os.getcwd()

data_folder = os.path.join(current_folder, 'tutorial_data', 'mnist')
root_folder = os.path.join(current_folder, 'tutorial_files', 'mnist')
image_file_train = os.path.join(data_folder, "train-images-idx3-ubyte")
label_file_train = os.path.join(data_folder, "train-labels-idx1-ubyte")
image_file_test = os.path.join(data_folder, "t10k-images-idx3-ubyte")
label_file_test = os.path.join(data_folder, "t10k-labels-idx1-ubyte")


# Get the dataset if it is missing
def DownloadDataset(url, path):
    import requests, zipfile, StringIO
    print
    "Downloading... ", url, " to ", path
    r = requests.get(url, stream=True)
    z = zipfile.ZipFile(StringIO.StringIO(r.content))
    z.extractall(path)


def GenerateDB(image, label, name):
    name = os.path.join(data_folder, name)
    print
    'DB: ', name
    if not os.path.exists(name):
        syscall = "/usr/local/binaries/make_mnist_db --channel_first --db leveldb --image_file " \
                  + image + " --label_file " + label + " --output_file " + name
        print
        "Creating database with: ", syscall
        os.system(syscall)
    else:
        print
        "Database exists already. Delete the folder if you have issues/corrupted DB, then rerun this."
        if os.path.exists(os.path.join(name, "LOCK")):
            print
            "Deleting the pre-existing lock file"
            os.remove(os.path.join(name, "LOCK"))


if not os.path.exists(data_folder):
    os.makedirs(data_folder)
if not os.path.exists(label_file_train):
    DownloadDataset("https://s3.amazonaws.com/caffe2/datasets/mnist/mnist.zip", data_folder)

if os.path.exists(root_folder):
    print("Looks like you ran this before, so we need to cleanup those old files...")
    shutil.rmtree(root_folder)

os.makedirs(root_folder)
workspace.ResetWorkspace(root_folder)

# (Re)generate the levledb database (known to get corrupted...)
GenerateDB(image_file_train, label_file_train, "mnist-train-nchw-leveldb")
GenerateDB(image_file_test, label_file_test, "mnist-test-nchw-leveldb")

print("training data folder:" + data_folder)
print("workspace root folder:" + root_folder)

def AddInput(model, batch_size, db, db_type):
    # load the data
    data_uint8, label = model.TensorProtosDBInput(
        [], ["data_uint8", "label"], batch_size=batch_size,
        db=db, db_type=db_type)
    # cast the data to float
    data = model.Cast(data_uint8, "data", to=core.DataType.FLOAT)
    # scale data from [0,255] down to [0,1]
    data = model.Scale(data, data, scale=float(1./256))
    # don't need the gradient for the backward pass
    data = model.StopGradient(data, data)
    return data, label

def AddLeNetModel(model, data):
    # Image size: 28 x 28 -> 24 x 24
    conv1 = brew.conv(model, data, 'conv1', dim_in=1, dim_out=20, kernel=5)
    # Image size: 24 x 24 -> 12 x 12
    pool1 = brew.max_pool(model, conv1, 'pool1', kernel=2, stride=2)
    # Image size: 12 x 12 -> 8 x 8
    conv2 = brew.conv(model, pool1, 'conv2', dim_in=20, dim_out=50, kernel=5)
    # Image size: 8 x 8 -> 4 x 4
    pool2 = brew.max_pool(model, conv2, 'pool2', kernel=2, stride=2)
    # 50 * 4 * 4 stands for dim_out from previous layer multiplied by the image size
    fc3 = brew.fc(model, pool2, 'fc3', dim_in=50 * 4 * 4, dim_out=500)
    fc3 = brew.relu(model, fc3, fc3)
    pred = brew.fc(model, fc3, 'pred', 500, 10)
    softmax = brew.softmax(model, pred, 'softmax')
    return softmax

def AddAccuracy(model, softmax, label):
    accuracy = brew.accuracy(model, [softmax, label], "accuracy")
    return accuracy

def AddTrainingOperators(model, softmax, label):
    # something very important happens here
    xent = model.LabelCrossEntropy([softmax, label], 'xent')
    # compute the expected loss
    loss = model.AveragedLoss(xent, "loss")
    # track the accuracy of the model
    AddAccuracy(model, softmax, label)
    # use the average loss we just computed to add gradient operators to the model
    model.AddGradientOperators([loss])
    # do a simple stochastic gradient descent
    ITER = brew.iter(model, "iter")
    # set the learning rate schedule
    LR = model.LearningRate(
        ITER, "LR", base_lr=-0.1, policy="step", stepsize=1, gamma=0.999 )
    # ONE is a constant value that is used in the gradient update. We only need
    # to create it once, so it is explicitly placed in param_init_net.
    ONE = model.param_init_net.ConstantFill([], "ONE", shape=[1], value=1.0)
    # Now, for each parameter, we do the gradient updates.
    for param in model.params:
        # Note how we get the gradient of each parameter - CNNModelHelper keeps
        # track of that.
        param_grad = model.param_to_grad[param]
        # The update is a simple weighted sum: param = param + param_grad * LR
        model.WeightedSum([param, ONE, param_grad, LR], param)
    # let's checkpoint every 20 iterations, which should probably be fine.
    # you may need to delete tutorial_files/tutorial-mnist to re-run the tutorial
    model.Checkpoint([ITER] + model.params, [],
                   db="mnist_lenet_checkpoint_%05d.leveldb",
                   db_type="leveldb", every=20)

    arg_scope = {"order": "NCHW"}
    train_model = model_helper.ModelHelper(name="mnist_train", arg_scope=arg_scope)
    data, label = AddInput(
        train_model, batch_size=64,
        db=os.path.join(data_folder, 'mnist-train-nchw-leveldb'),
        db_type='leveldb')
    softmax = AddLeNetModel(train_model, data)
    AddTrainingOperators(train_model, softmax, label)

    # Testing model. We will set the batch size to 100, so that the testing
    # pass is 100 iterations (10,000 images in total).
    # For the testing model, we need the data input part, the main LeNetModel
    # part, and an accuracy part. Note that init_params is set False because
    # we will be using the parameters obtained from the train model.
    test_model = model_helper.ModelHelper(
        name="mnist_test", arg_scope=arg_scope, init_params=False)
    data, label = AddInput(
        test_model, batch_size=100,
        db=os.path.join(data_folder, 'mnist-test-nchw-leveldb'),
        db_type='leveldb')
    softmax = AddLeNetModel(test_model, data)
    AddAccuracy(test_model, softmax, label)

    # Deployment model. We simply need the main LeNetModel part.
    deploy_model = model_helper.ModelHelper(
        name="mnist_deploy", arg_scope=arg_scope, init_params=False)
    AddLeNetModel(deploy_model, "data")

    graph = net_drawer.GetPydotGraphMinimal(
        train_model.net.Proto().op, "mnist", rankdir="LR", minimal_dependency=True)
    display.Image(graph.create_png(), width=800)

    with open(os.path.join(root_folder, "train_net.pbtxt"), 'w') as fid:
        fid.write(str(train_model.net.Proto()))
    with open(os.path.join(root_folder, "train_init_net.pbtxt"), 'w') as fid:
        fid.write(str(train_model.param_init_net.Proto()))
    with open(os.path.join(root_folder, "test_net.pbtxt"), 'w') as fid:
        fid.write(str(test_model.net.Proto()))
    with open(os.path.join(root_folder, "test_init_net.pbtxt"), 'w') as fid:
        fid.write(str(test_model.param_init_net.Proto()))
    with open(os.path.join(root_folder, "deploy_net.pbtxt"), 'w') as fid:
        fid.write(str(deploy_model.net.Proto()))
    print("Protocol buffers files have been created in your root folder: " + root_folder)

    # The parameter initialization network only needs to be run once.
    workspace.RunNetOnce(train_model.param_init_net)
    # creating the network
    workspace.CreateNet(train_model.net)
    # set the number of iterations and track the accuracy & loss
    total_iters = 200
    accuracy = np.zeros(total_iters)
    loss = np.zeros(total_iters)
    # Now, we will manually run the network for 200 iterations.
    for i in range(total_iters):
        workspace.RunNet(train_model.net.Proto().name)
        accuracy[i] = workspace.FetchBlob('accuracy')
        loss[i] = workspace.FetchBlob('loss')
    # After the execution is done, let's plot the values.
    pyplot.plot(loss, 'b')
    pyplot.plot(accuracy, 'r')
    pyplot.legend(('Loss', 'Accuracy'), loc='upper right')

    # Let's look at some of the data.
    pyplot.figure()
    data = workspace.FetchBlob('data')
    _ = visualize.NCHW.ShowMultiple(data)
    pyplot.figure()
    softmax = workspace.FetchBlob('softmax')
    _ = pyplot.plot(softmax[0], 'ro')
    pyplot.title('Prediction for the first image')

    # run a test pass on the test net
    workspace.RunNetOnce(test_model.param_init_net)
    workspace.CreateNet(test_model.net)
    test_accuracy = np.zeros(100)
    for i in range(100):
        workspace.RunNet(test_model.net.Proto().name)
        test_accuracy[i] = workspace.FetchBlob('accuracy')
    # After the execution is done, let's plot the values.
    pyplot.plot(test_accuracy, 'r')
    pyplot.title('Acuracy over test batches.')
    print('test_accuracy: %f' % test_accuracy.mean())