## Running SHARK Locally

nod.ai SHARK is a High Performance Machine Learning Distribution that can run locally on your machine. Simply download the latest release from the [SHARK GitHub](https://github.com/nod-ai/SHARK/releases)

### For Windows

1. Download the `.exe` file
2. Run `python shark_sd_<date>_<version>.exe --api --server_port=8080`
3. This should run the application on localhost port 8080.
4. To use SHARK Stable Diffusion on Blender:
   - On Blender, go to Edit > Preferences > AI-Render 
   - Select SHARK as the Stable Diffusion Backend
   - Change the Local Web Server URL to use the correct port number

For more detailed installation notes check out the [SHARK GitHub](https://github.com/nod-ai/SHARK).

## Troubleshooting

- Ensure that you followed the steps to set up SHARK correctly: [Installation Steps](https://github.com/AyaanShah2204/AI-Render/wiki/SHARK-by-nod.ai#running-shark-locally)
- Make sure you are running a stable release of SHARK: [SHARK Releases](https://github.com/nod-ai/SHARK/releases)
- At the time of writing **SHARK's Upscaler API is down.** Try running with auto-upscaling turned off in the Upscale settings
- For additional guidance, please refer to the [SHARK GitHub](https://github.com/nod-ai/SHARK) or join our [Discord!](https://discord.gg/RUqY2h2s9u)