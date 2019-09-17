# Kitti3 - Kitty drop-down service for i3wm
Kitti3 turns [Kitty](https://sw.kovidgoyal.net/kitty/) into a drop-down, Quake-style 
floating terminal for the [i3 window manager](https://i3wm.org/).

#### Features
- i3 native, *flicker-free* visibility toggling 
- Multi-monitor support with adaptive resizing to active monitor resolution
- Position the terminal along any screen edge
- Great responsiveness by leveraging i3's IPC API
- Support for multiple instances
- Kitty argument forwarding (e.g. `--session`)

[TODO: image of kitti3]

Disclaimer: no screen redraws are wasted on actually animating dropping the terminal down 
into view; it just appears and disappears on demand.

## Installation and setup
Kitti3 is a Python 3 package that [lives on PYPI](TODO). 
- To install it, either:
    1. use pip:
        ```commandline
        $ pip install kitti3 --user
        ```
    2. or copy [main.py](https://github.com/LandingEllipse/Kitti3/blob/master/src/kitti3/main.py)
    to somewhere on your $PATH, rename it to `kitti3` and make it executable. (*Note:*
    in this case it's your responsibility satisfy the [dependencies](#dependencies)) 

- Ensure that Kitti3 is reachable (e.g. `$ which kitti3`); i3 won't necessarily complain later 
on if it isn't!

- Add the following to your `~/.config/i3/config`:
    ```commandline
    exec --no-startup-id kitti3
    bindsym $mod+n nop kitti3
    ```
    where `$mod+n` refers to your keyboard shortcut of choice. Take a look at the 
    [configuration](#configuration) below for a list of the parameters that Kitti3 accepts.
    
- Restart i3wm

- Trigger the shortcut to verify that the terminal appears (slight flicker / tiling 
noise is normal on the first toggle when Kitty is spawned and floated by Kitti3)


## Configuration
Kitti3 doesn't make use of a dedicated configuration file, but the default behaviour can 
be changed via commandline arguments:
```commanline
TODO: WIP
```

### Multiple instances
```commandline
exec --no-startup-id kitti3 -n bubblegum
bindsym $mod+n nop bubblegum
    ```



## Dependencies
- [Kitty](https://sw.kovidgoyal.net/kitty/) (duh)
- i3 (tested with 4.17 but if you're stuck in the past it's probably fine on 3.xx)
- Python >= 3.6 (because f-strings; fork and substitute if you need compatibility)
- [i3ipc-python](https://github.com/altdesktop/i3ipc-python) (pip will pull in >=2.0)

## Alternatives
### The natives
If you're not too fussed about which terminal you're using then there are several 
alternatives out there that do drop-down out of the box, like 
[guake](http://guake-project.org/) and [tilda](https://github.com/lanoxx/tilda). However, 
if you find yourself wanting to experiment with fonts that support programming ligatures 
(like the excellent [FiraCode](https://github.com/tonsky/FiraCode)), your options 
quickly dwindle as terminals based on the VTE library (like the two above) still don't 
play well with ligatures.

### The other bolt-ons
But you're here because you want to use Kitty, so forget about the natives for a second
and instead ask yourself why you shouldn't just be using one of the other "drop-downifiers".
Two notable mentions in this space are [tdrop](https://github.com/noctuid/tdrop) and 
[i3-quickterm](https://github.com/lbonn/i3-quickterm). tdrop is a swiss army knife
that could probably turn a potato into a drop-down if you worked hard enough, but while
feature rich it can be prohibitively slow and cause substantial flicker artifacts in i3
during visibility toggling. 

Kitti3 was actually inspired by the approach taken by i3-quickterm, which issues 
show/hide commands to i3 via IPC. It also supports other terminals than just Kitty, 
however its single-shot, mark-based design leads to some speed penalties and unwanted 
behaviour when spawning terminals. If you're open to using other terminals than Kitty 
(and have somehow made it this far into the readme), you should try it out. It was 
i3-quickterm's inability to display terminals as slide-ins (as opposed to drop-down or 
pop-up) that prompted the creation of Kitti3.
 
Kitti3 runs as a daemon and listens to events through i3's IPC API, using information
about the active workspace to dynamically direct i3 in how to best resize and position 
Kitty when visibility is toggled. This leads to excellent responsiveness and no flicker 
artifacts, as well as a seamless experience in multi-monitor, multi-resolution setups.

### i3wm config
*"But I don't have a hundred external monitors on my desk!"* you cry out. Well, if you're
running a single-monitor setup, or you're simply content with having the terminal 
displayed on your main monitor only, then you don't actually need Kitti3 or any of the 
other bolt-ons. i3 is happy to take care of container floating and positioning if you're 
happy to work with absolute pixel values. This is where you start (add to 
`~/.config/i3/config`):
```commandline
exec --no-startup-id kitty --name dropdown 
for_window [instance="dropdown"] floating enable, border none, move absolute \
position 0px 0px, resize set 1920px 384px, move scratchpad
bindsym $mod+n [instance="dropdown"] scratchpad show
```
and the [i3 user's guide](https://i3wm.org/docs/userguide.html) will lead you the rest 
of the way.

## Development
Found a bug? Have a feature request? Create an issue on GitHub!

Want to get your hands dirty and contribute? Great! Clone the repository and dig in.

The project adopts a `setuptools` based structure and can be installed in 
development mode using pip (from the project root directory):
    
    $ pip install -e .

This exposes the `kitti3` entry point script, which starts the Kitty service.

## License
Kitti3 is released under a BSD 3-clause license; see [LICENCE](https://github.com/LandingEllipse/Kitti3/blob/master/LICENSE) for the details.
