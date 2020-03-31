import torch
from itertools import count
import types
from .random import Distribution, Uniform, Uniform2D, Bernoulli, Categorical


class AugmentationFactory(torch.nn.Module):
    """Generates deterministic augmentations.

    It is a serializable generator that can override default random parameters.

    """
    def __init__(self, augmentation_cls, **kwargs):
        super().__init__()
        assert set(kwargs.keys()) <= set(augmentation_cls.distributions.keys())
        self.augmentation_distributions = {k: v.copy() for k, v in augmentation_cls.distributions.items()}
        overridden_distributions = {k: v for k, v in kwargs.items() if k in augmentation_cls.distributions.keys()}
        self.augmentation_distributions.update(overridden_distributions)
        self.augmentation_cls = augmentation_cls
        for d_name, distribution in self.augmentation_distributions.items():
            for p_name, p_value in distribution.parameters().items():
                parameter_name = f"{d_name}_{p_name}"
                self.register_parameter(parameter_name, torch.nn.Parameter(p_value))

    def forward(self, x):
        if self.training:
            return self.new()(x)
        else:
            return x

    def new(self):
        return self.augmentation_cls(**self.augmentation_distributions)



class DeterministicImageAugmentation(object):
    """Deterministic augmentation functor and its factory.

    This class is the base class realising an augmentation.
    In order to create a new augmentation the forward_sample_img method and the factory class-function have to be defined.
    If a constructor is defined it must call super().__init__(**kwargs).

    The **kwargs on the constructor define all needed variables for generating random augmentations.
    The factory method returns a lambda that instanciates augmentatations.
    Any parameter about the augmentations randomness should be passed by the factory to the constructor and used inside
    forward_sample_img's definition.
    """

    _ids = count(0)
    # since instance seeds are the global seed plus the instance counter, starting from 1000000000 makes a collision
    # highly improbable. Although no guaranty of uniqueness of instance seed is made.
    # in case of a multiprocess parallelism, this minimizes chances of collision
    _global_seed = torch.LongTensor(1).random_(1000000000, 2000000000).item()
    distributions = {"occurence": Bernoulli(prob=.3)}

    @staticmethod
    def reset_all_seeds(global_seed=None):
        SpatialImageAugmentation._ids = count(0)
        if global_seed is None:
            global_seed = 0
        SpatialImageAugmentation._global_seed = global_seed


    def __init__(self, id, seed):
        if id is None:
            self.id = next(SpatialImageAugmentation._ids)
        else:
            self.id = id
        if seed is None:
            self.seed = self.id + SpatialImageAugmentation._global_seed
        else:
            self.seed = seed

    def __call__(self, tensor_image):
        device = tensor_image.device
        with torch.random.fork_rng(devices=(device,)):
            torch.manual_seed(self.seed)
            if len(tensor_image.size()) == 3:
                if self.occurence():
                    return self.forward_sample_img(tensor_image)
                else:
                    return tensor_image
            elif len(tensor_image.size()) == 4:
                # faster but not differentiable
                #apply_on = self.occurence(tensor_image.size(0)).byte()
                #applied = self.forward_batch_img(tensor_image[apply_on, :, :, :])
                #results = tensor_image.clone()
                #results[apply_on, :, :, :] = applied
                #return results

                #slower but differentiable
                applied = self.forward_batch_img(tensor_image)
                mask = self.occurence(tensor_image.size(0)).unsqueeze(dim=1).unsqueeze(dim=1).unsqueeze(dim=1)
                return applied * mask + tensor_image * (1 - mask)

            else:
                raise ValueError("Augmented tensors must be samples (3D tensors) or batches (4D tensors)")

    def __repr__(self):
        param_names = [f"{k}" for k in self.distributions.keys()]
        param_names = ("id", "seed") + tuple(param_names)
        param_assignments = ", ".join(["{}={}".format(k, repr(self.__dict__[k])) for k in param_names])
        return self.__class__.__qualname__ + "(" + param_assignments + ")"

    def __eq__(self, obj):
        return self.__class__ is obj.__class__ and self.id == obj.id and self.seed == obj.seed

    def __str__(self):
        functions = (types.BuiltinFunctionType, types.BuiltinMethodType, types.FunctionType)
        attribute_strings = []
        for name in self.__dict__.keys():
            attribute = getattr(self, name)
            if (not isinstance(attribute, functions)) and (not name.startswith("__")):
                attribute_strings.append(f"{name}:{repr(attribute)}")
        return self.__class__.__qualname__ + ":\n\t" + "\n\t".join(attribute_strings)


    def forward_sample_img(self, tensor_image):
        """Distorts a single image.

        Abstract method that must be implemented by all augmentations.

        :param tensor_image: Images are 3D tensors of [#channels, width, height] size.
        :return: A new 3D tensor [#channels, width, height] with the new image.
        """
        assert type(self).forward_batch_img is not DeterministicImageAugmentation.forward_batch_img
        return self.forward_batch_img(tensor_image.unsqueeze(dim=0))[0, :, :, :]


    def forward_batch_img(self, batch_tensor):
        """Distorts a a batch of one or more images.

        :param batch_tensor: Images are 4D tensors of [batch_size, #channels, width, height] size.
        :return: A new 4D tensor [batch_size, #channels, width, height] with the new image.
        """
        assert type(self).forward_batch_img is not DeterministicImageAugmentation.forward_sample_img
        augmented_tensors = []
        for n in range(batch_tensor.size(0)):
            augmented_tensors.append(self.forward_sample_img(batch_tensor[n, :, :, :]))
        return torch.cat(augmented_tensors, dim=0)

    def forward_sample_points(self, point_cloud):
        """Distorts a point cloud.
        """
        raise NotImplementedError()

    def forward_batch_points(self, point_cloud):
        """Distorts many point clouds.
        """
        raise NotImplementedError()


    @classmethod
    def factory(cls, **kwargs):
        return AugmentationFactory(cls, **kwargs)


    @property
    def preserves_geometry(self):
        raise NotImplementedError()


def aug_parameters(**kwargs):
    """Decorator that assigns random parameter ranges to augmentation and creates the augmentation's constructor.

    """
    default_params = kwargs

    def register_default_random_parameters(cls):
        setattr(cls, "default_params", default_params.copy())

        # Creating dynamically a constructor
        rnd_param = "\n\t".join((f"self.{k} = {k}" for k, v in default_params.items()))
        default_params["id"] = None
        default_params["seed"] = None
        param_str = ", ".join((f"{k}={repr(v)}" for k, v in default_params.items()))
        constructor_str = f"def __init__(self, {param_str}):\n\tDeterministicImageAugmentation.__init__(self, id=id, seed=seed)\n\t{rnd_param}"
        exec_locals = {"__class__": cls}
        #print(constructor_str)
        exec(constructor_str, globals(), exec_locals)
        setattr(cls, "__init__", exec_locals["__init__"])
        return cls
    return register_default_random_parameters


def aug_distributions(occurence = Bernoulli(.5), **kwargs):
    """Decorator that assigns random parameter ranges to augmentation and creates the augmentation's constructor.

    """
    # assert all([isinstance(v, Distribution) for v in kwargs.values()])
    # TODO (anguelos) howto handle non distribution factory parameters?

    default_params = kwargs
    default_params.update({"occurence": occurence})

    def register_distributions(cls):
        new_distributions = getattr(cls, "distributions").copy()
        new_distributions.update(default_params.copy())
        setattr(cls, "distributions", new_distributions)
        # Creating dynamically a constructor
        rnd_param = "\n\t".join((f"self.{k}={k}" for k in default_params.keys()))
        default_params["id"] = None
        default_params["seed"] = None
        param_str = ", ".join((f"{k}={k}" for k, v in default_params.items()))
        constructor_str = f"def __init__(self, {param_str}):\n\tDeterministicImageAugmentation.__init__(self, id=id, seed=seed)\n\t{rnd_param}"
        exec_locals = {"__class__": cls}
        exec_locals.update(default_params)
        exec(constructor_str, globals(), exec_locals)
        setattr(cls, "__init__", exec_locals["__init__"])
        return cls
    return register_distributions


class ChannelImageAugmentation(DeterministicImageAugmentation):
    @property
    def preserves_geometry(self):
        return True


class SpatialImageAugmentation(DeterministicImageAugmentation):
    @property
    def preserves_geometry(self):
        return False
