using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows.Forms;
using GxSequencerSample;

namespace GxGetImage
{
    static class Program
    {
        /// <summary>
        /// The main entry point for the application.
        /// </summary>
        [STAThread]
        static void Main()
        {
            GxSequencerSampleEvent objSequencerSample = new GxSequencerSampleEvent();
            objSequencerSample.__InitDevice();
        }
    }
}
