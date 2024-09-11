import torch
from torch import FloatTensor, LongTensor, Tensor, Size, lerp, zeros_like
from torch.linalg import norm

from diffusers import StableDiffusionPipeline
import librosa
import tqdm as tqdm
from PIL import Image
import numpy as np
import moviepy.editor as mpy
import math




class NoiseVisualizer:
    def __init__(self, device="mps", weightType=torch.float16,seed=42069):
        torch.manual_seed(seed)
        self.device = device
        self.weightType = weightType
        self.pipe = StableDiffusionPipeline.from_pretrained("IDKiro/sdxs-512-dreamshaper", torch_dtype=weightType, disable_progress_bar=True)
        self.textEncoder = self.pipe.text_encoder
        self.tokenizer = self.pipe.tokenizer
        
    def loadSong(self,file,hop_length=512):
        y, sr = librosa.load(file) # 3 min 52 sec
        self.hop_length=hop_length
        self.sr = sr
        self.y = y
        
        y_harmonic, y_percussive = librosa.effects.hpss(y)
        
        # Latent generaton
        
        self.beat, self.beat_frames = librosa.beat.beat_track(y=y_percussive, sr=sr,hop_length=self.hop_length)
        
        melSpec = librosa.feature.melspectrogram(y=y_percussive,sr=sr, n_mels = 256, hop_length=self.hop_length)
        self.melSpec = np.sum(melSpec,axis=0)
        
        
        # Prompt Generation
        self.chroma = librosa.feature.chroma_cqt(y=y_harmonic, sr=sr, hop_length=self.hop_length)
        
        self.chromaGrad = np.gradient(self.chroma)
        
        self.steps = self.melSpec.shape[0]
        
    def getEasedBeats(self, noteType):
        def cubic_in_out_numpy(t):
            return np.where(t < 0.5, 4 * t**3, 1 - (-2 * t + 2)**3 / 2)

        if noteType == "half":
            beat_frames = self.beat_frames[::2]
        elif noteType == "whole":
            beat_frames = self.beat_frames[::4]
        else:
            beat_frames = self.beat_frames
            
        # Initialize output array
        output_array = np.zeros(self.steps, float)

        # Normalize the mel spectrogram values at the beat frames and set them in the output array
        output_array[beat_frames] = librosa.util.normalize(self.melSpec[beat_frames])

        # Interpolate between the beat frames
        last_beat_idx = None
        for current_idx in beat_frames:
            if last_beat_idx is not None:
                # Interpolate between the previous beat and current beat
                for j in range(last_beat_idx + 1, current_idx):
                    t = (j - last_beat_idx) / (current_idx - last_beat_idx)
                    eased_value = cubic_in_out_numpy(np.array(t))
                    output_array[j] = eased_value
            last_beat_idx = current_idx

        return torch.tensor(output_array, dtype=self.weightType)
    
    def getBeatLatents(self, distance, noteType): 
        # shape: (batch, latent channels, height, width)
        shape = (1, 4, 64, 64)
        
        # Initialize random noise for X and Y coordinates of the walk
        walkNoiseX = torch.randn(shape, dtype=self.weightType, device=self.device)
        walkNoiseY = torch.randn(shape, dtype=self.weightType, device=self.device)
        
        # Generate cosine and sine scales for the circular walk
        walkScaleX = torch.cos(torch.linspace(0, distance, self.steps, dtype=self.weightType, device=self.device) * math.pi)
        walkScaleY = torch.sin(torch.linspace(0, distance, self.steps, dtype=self.weightType, device=self.device) * math.pi)
        
        # Apply the easing values to the scales (mapping easing values to noise)
        easing_values = self.getEasedBeats(noteType=noteType).to(self.device)
        
        walkScaleX = walkScaleX * easing_values
        walkScaleY = walkScaleY * easing_values
        
        # Generate the noise tensors based on the interpolated scales
        noiseX = torch.tensordot(walkScaleX, walkNoiseX, dims=0).to(self.device)
        noiseY = torch.tensordot(walkScaleY, walkNoiseY, dims=0).to(self.device)
        
        # Add the noise contributions for X and Y to create the latent walk
        latents = torch.add(noiseX, noiseY)
        latents = latents.squeeze(1)
        return latents

    def getFPS(self):
        return self.sr / self.hop_length
    
    def slerp(self, v0: FloatTensor, v1: FloatTensor, t: float|FloatTensor, DOT_THRESHOLD=0.9995):
        '''
        Spherical linear interpolation
        Args:
            v0: Starting vector
            v1: Final vector
            t: Float value between 0.0 and 1.0
            DOT_THRESHOLD: Threshold for considering the two vectors as
                                    colinear. Not recommended to alter this.
        Returns:
            Interpolation vector between v0 and v1
        '''

        assert v0.shape == v1.shape, "shapes of v0 and v1 must match"

        # Normalize the vectors to get the directions and angles
        v0_norm: FloatTensor = norm(v0, dim=-1)
        v1_norm: FloatTensor = norm(v1, dim=-1)

        v0_normed: FloatTensor = v0 / v0_norm.unsqueeze(-1)
        v1_normed: FloatTensor = v1 / v1_norm.unsqueeze(-1)

        # Dot product with the normalized vectors
        dot: FloatTensor = (v0_normed * v1_normed).sum(-1)
        dot_mag: FloatTensor = dot.abs()

        # if dp is NaN, it's because the v0 or v1 row was filled with 0s
        # If absolute value of dot product is almost 1, vectors are ~colinear, so use lerp
        gotta_lerp: LongTensor = dot_mag.isnan() | (dot_mag > DOT_THRESHOLD)
        can_slerp: LongTensor = ~gotta_lerp

        t_batch_dim_count: int = max(0, t.dim()-v0.dim()) if isinstance(t, Tensor) else 0
        t_batch_dims: Size = t.shape[:t_batch_dim_count] if isinstance(t, Tensor) else Size([])
        out: FloatTensor = zeros_like(v0.expand(*t_batch_dims, *[-1]*v0.dim()))

        # if no elements are lerpable, our vectors become 0-dimensional, preventing broadcasting
        if gotta_lerp.any():
            lerped: FloatTensor = lerp(v0, v1, t)

            out: FloatTensor = lerped.where(gotta_lerp.unsqueeze(-1), out)

        # if no elements are slerpable, our vectors become 0-dimensional, preventing broadcasting
        if can_slerp.any():

            # Calculate initial angle between v0 and v1
            theta_0: FloatTensor = dot.arccos().unsqueeze(-1)
            sin_theta_0: FloatTensor = theta_0.sin()
            # Angle at timestep t
            theta_t: FloatTensor = theta_0 * t
            sin_theta_t: FloatTensor = theta_t.sin()
            # Finish the slerp algorithm
            s0: FloatTensor = (theta_0 - theta_t).sin() / sin_theta_0
            s1: FloatTensor = sin_theta_t / sin_theta_0
            slerped: FloatTensor = s0 * v0 + s1 * v1

            out: FloatTensor = slerped.where(can_slerp.unsqueeze(-1), out)
        
        return out


    
    def getPromptEmbeds(self, basePrompt, targetPromptChromaScale, method="linear", alpha=0.5, decay_rate=0.7, boost_factor=2.5, boost_threshold=0.5):
        chroma = self.chroma.T  # shape-> n,12
        numFrames = chroma.shape[0]
        
        # Initialize weights and boost tracker
        chromaW = chroma / np.sum(chroma, axis=1, keepdims=True)
        chromaBoost = np.ones(chroma.shape)  # Initialize boost factors to 1
        
        # Initialize history of dominance for each chroma
        chromaDominance = np.zeros_like(chroma)
        
        for frameNum in range(1, numFrames):
            for chromaKey in range(12):
                if chroma[frameNum, chromaKey] > chroma[frameNum - 1, chromaKey]:  # Check if chroma is increasing
                    chromaDominance[frameNum, chromaKey] = chromaDominance[frameNum - 1, chromaKey] + 1
                else:
                    chromaDominance[frameNum, chromaKey] = chromaDominance[frameNum - 1, chromaKey] * decay_rate
            
            # Determine the dominant chroma
            dominant_chroma = np.argmax(chroma[frameNum])
            
            # Boost less active chromas that have been inactive for a while
            for chromaKey in range(12):
                if chromaDominance[frameNum, chromaKey] < chromaDominance[frameNum, dominant_chroma]:
                    if chroma[frameNum, chromaKey] > boost_threshold:
                        chromaBoost[frameNum, chromaKey] = boost_factor
        
        # Apply the boost to chroma weights
        chromaW = chromaW * chromaBoost
        chromaW = chromaW / np.sum(chromaW, axis=1, keepdims=True)  # Re-normalize to ensure weights sum to 1
        
        chromaW = torch.from_numpy(chromaW)
        chromaW = chromaW.view(-1, 12, 1, 1)  # chroma weighted, all adding to 1 for every slice.
        
        alphas = np.full(chroma.shape, alpha)
        chroma = chroma * alphas  # defines maximum value the chroma can take on, depending on alpha
        
        baseInput = self.tokenizer(basePrompt, return_tensors="pt", padding="max_length", truncation=True, max_length=77).input_ids
        targetInput = self.tokenizer(targetPromptChromaScale, return_tensors="pt", padding="max_length", truncation=True, max_length=77).input_ids
        
        baseEmbeds = self.textEncoder(baseInput)[0]  # shape-> 1,3,768
        targetEmbeds = self.textEncoder(targetInput)[0]  # shape-> 12,3,768
        
        targetEmbeds = targetEmbeds.unsqueeze(0).expand(numFrames, -1, -1, -1)  # shape -> n, 12, 3, 768
        
        interpolatedEmbedsAll = []
        for frameNum in range(numFrames):  # add all with chroma mag
            interpolatedEmbed = baseEmbeds.clone()  # shape 1,16,768
            interpolatedEmbed = interpolatedEmbed.squeeze(0)
            interpolatedEmbedsWeighted = []
            for chromaKey in range(12):
                if method == "linear":
                    interpolatedEmbedUnweighted = (1 - chroma[frameNum, chromaKey]) * interpolatedEmbed + \
                                                chroma[frameNum, chromaKey] * targetEmbeds[frameNum, chromaKey]
                elif method == "slerp":
                    interpolatedEmbedUnweighted = self.slerp(interpolatedEmbed, targetEmbeds[frameNum, chromaKey], chroma[frameNum, chromaKey]) # check 0.5
                
                interpolatedEmbedsWeighted.append(interpolatedEmbedUnweighted)
            
            interpolatedEmbedsWeighted = torch.stack(interpolatedEmbedsWeighted).squeeze(1)
            
            interpolatedEmbed = torch.sum(interpolatedEmbedsWeighted * chromaW[frameNum], dim=0).unsqueeze(0)
            
            interpolatedEmbedsAll.append(interpolatedEmbed)
        
        interpolatedEmbeds = torch.stack(interpolatedEmbedsAll).float()
        
        return interpolatedEmbeds
        
        
      
    def getVisuals(self, latents, promptEmbeds, num_inference_steps=1,guidance_scale = 0, batch_size=1):
        #self.pipe.vae = self.vae
        self.pipe.to(device=self.device, dtype=self.weightType)
        
        latents.to(self.device)
        promptEmbeds.to(self.device)
        
        allFrames=[]
        
        num_frames = latents.shape[0]
        
        for i in tqdm.tqdm(range(0, num_frames, batch_size)):
            frames = self.pipe(prompt_embeds=promptEmbeds[i],
                       guidance_scale=guidance_scale,
                       num_inference_steps=num_inference_steps,
                       latents=latents[i:i+batch_size],
                       output_type="pil").images
            allFrames.extend(frames)
        return allFrames
        
        