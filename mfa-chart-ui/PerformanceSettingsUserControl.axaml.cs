using Avalonia.Controls;
using MFAAvalonia.ViewModels.UsersControls.Settings;

namespace MFAAvalonia.Views.UserControls.Settings;

public partial class PerformanceSettingsUserControl : UserControl
{
    public PerformanceSettingsUserControl()
    {
        var model = PerformanceSettingsUserControlModel.Shared;
        DataContext = model;
        InitializeComponent();
        Loaded += (_, _) => model.Activate();
        Unloaded += (_, _) => model.Deactivate();
    }
}
