using Avalonia.Controls;
using MFAAvalonia.ViewModels.UsersControls.Settings;

namespace MFAAvalonia.Views.UserControls.Settings;

public partial class PerformanceSettingsUserControl : UserControl
{
    public PerformanceSettingsUserControl()
    {
        var model = new PerformanceSettingsUserControlModel();
        DataContext = model;
        InitializeComponent();
        Loaded += (_, _) => model.RefreshCommand.Execute(null);
    }
}
